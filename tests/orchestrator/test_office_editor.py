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
const calls = [], messages = [], scripts = [], configs = [], order = [], actionStates = [], actionResources = [];
const listeners = new Map(), timers = new Map();
let timerId = 0, attempts = 0, destroyed = 0, resolveCreate, resolveSave, resolveClose, resolveBody, pendingLoad;
let now = 0, activeStatuses = 0, maxActiveStatuses = 0, statusReads = 0;
const heldStatuses = [], signals = [];
const clone = value => JSON.parse(JSON.stringify(value));
const signed = {documentType:'word', document:{key:'stable-key',url:'http://private.test/office/source/ticket',fileType:'docx',permissions:{edit:true}},editorConfig:{callbackUrl:'http://private.test/office/callback/chat/session',mode:'edit',customization:{forcesave:false}},token:'signed-configuration-token'};
const initial = {session_id:'session-id',file_id:file,document_key:'stable-key',state:'editing',reason:null,save_seq:2,last_committed_seq:2,last_published_seq:2,workspace_changed:false,saved_as:null,...options.status};
let currentStatus = clone(initial);
const response = (status, body) => ({ok:status>=200&&status<300,status,json:async()=>{
  const kind=body?.editor_config!==undefined?'create':body?.workspace_changed!==undefined?'status':
    body?.intent?'save':body?.session_id&&body?.state?'close':null;
  if(kind&&options.deferBody===kind)return new Promise(resolve=>{resolveBody=()=>resolve(clone(body));});
  return clone(body);
}});
const createReply=()=>response(options.joined?200:201,{session_id:'session-id',file_id:file,document_key:'stable-key',
  state:'opening',joined:Boolean(options.joined),editor_config:options.noConfig?null:clone(signed)});
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
              if(options.syncDispose)emit('pagehide');
              config.events.onDocumentReady?.({});
            }
            destroyEditor(){destroyed++;order.push('destroyEditor');}
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
},addEventListener(type,listener){order.push('listener:'+type);if(!listeners.has(type))listeners.set(type,new Set());listeners.get(type).add(listener);},
  removeEventListener(type,listener){const group=listeners.get(type);group?.delete(listener);if(group?.size===0)listeners.delete(type);},
  setTimeout(fn,ms){const id=++timerId;timers.set(id,{fn,ms,due:now+ms});return id;},clearTimeout(id){timers.delete(id);}};
const open={type:'ocu:office-open',chat_id:chat,file_id:file,generation:7};
function emit(type,event={}){for(const listener of [...(listeners.get(type)||[])])listener(event);}
function dispatch(data, changes={}){emit('message',{source:hostWindow.parent,origin:hostWindow.location.origin,data,...changes});}
function sendTestMessage(row){const changes={};if(row.source)changes.source={parent:row.source==='nested'?hostWindow:hostWindow.parent};if(row.origin)changes.origin=row.origin;dispatch(row.data,changes);}
const fetch=async(url,init={})=>{
  const method=init.method||'GET';const headers=Object.fromEntries(new Headers(init.headers));
  const body=init.body?JSON.parse(init.body):null;
  calls.push({url:String(url),method,headers,body});order.push('fetch:'+method);
  if(init.signal)signals.push(init.signal);
  if(method==='POST'&&String(url).endsWith('/save')){
    if(options.saveTransportError)throw new Error('save transport failed');
    if(options.rejectSave)return response(502,{reason:'documentserver_unavailable'});
    if(currentStatus.state!=='editing'){
      const refusal=response(409,{reason:'session_not_editing'});
      if(options.deferPersistRefusal&&body.intent==='persist')return new Promise(resolve=>{resolveSave=()=>resolve(refusal);});
      return refusal;
    }
    currentStatus={...currentStatus,state:'saving',save_seq:options.nextSaveSequence??currentStatus.save_seq+1,reason:null};
    if(options.deferPersistFailure&&body.intent==='persist')return new Promise(resolve=>{resolveSave=()=>{
      currentStatus={...currentStatus,state:'editing'};
      resolve(response(502,{reason:'documentserver_unavailable'}));
    };});
    if(options.autoCommit)currentStatus={...currentStatus,state:'editing',last_committed_seq:currentStatus.save_seq,
      last_published_seq:options.equalPublished?currentStatus.save_seq:currentStatus.last_published_seq};
    const reply=response(202,{session_id:'session-id',save_seq:currentStatus.save_seq,intent:body.intent});
    if(options.deferSave)return new Promise(resolve=>{resolveSave=()=>resolve(reply);});
    return reply;
  }
  if(method==='POST'&&String(url).endsWith('/close')){
    if(options.rejectClose)return response(503,{reason:'storage_low'});
    currentStatus={...currentStatus,save_seq:currentStatus.save_seq+1,
      state:currentStatus.state==='opening'?'closed':currentStatus.state==='conflict'?'conflict':'closing'};
    const reply=response(202,{session_id:'session-id',save_seq:currentStatus.save_seq,state:currentStatus.state});
    if(options.deferClose)return new Promise(resolve=>{resolveClose=()=>resolve(reply);});
    return reply;
  }
  if(method==='POST'){
    if(options.transportError)throw new Error('network failed');
    if(options.deferCreate)return new Promise(resolve=>{resolveCreate=()=>resolve(createReply());});
    if(options.refusal)return response(options.refusal.status,{reason:options.refusal.reason});
    return createReply();
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
const context=vm.createContext({window:hostWindow,document:hostDocument,URL,Headers,AbortController,fetch,console,
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
const module=await load('https://webui.test'+(options.prefix??'/tools/ocu')+'/static/office-editor.js');await module.evaluate();
const mount=()=>module.namespace.createOfficeEditorHost({chatId:chat,docserverOrigin:options.origin||'https://office.test:8443',container,window:hostWindow,document:hostDocument});
let host=mount();
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
if(options.messages){for(const row of options.messages)sendTestMessage(row);await idle();}
if(options.open)dispatch(open);
await idle();
if(resolveCreate&&options.releaseCreate){resolveCreate();await idle();}
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
  else if(action.kind==='releaseClose')resolveClose?.();
  else if(action.kind==='allowClose')options.rejectClose=false;
  else if(action.kind==='pagehide')emit('pagehide');
  else if(action.kind==='releaseCreate')resolveCreate?.();
  else if(action.kind==='releaseBody'){options.deferBody=null;resolveBody?.();}
  else if(action.kind==='lateApi'){hostWindow.DocsAPI={DocEditor:class{constructor(){attempts++;}destroyEditor(){destroyed++;}}};pendingLoad?.();}
  else if(action.kind==='remount'){currentStatus=clone(initial);host=mount();dispatch(open);}
  else if(action.kind==='dispose')host.dispose();
  else if(action.kind==='error')configs.at(-1).events.onError({data:{errorCode:-18}});
  else if(action.kind==='allowStatus')options.holdStatusAfter=undefined;
  else if(action.kind==='allowSave'){options.rejectSave=false;options.saveTransportError=false;}
  else if(action.kind==='message')sendTestMessage(action);
  else throw new Error('Unknown test action: '+action.kind);
  await idle();
  actionStates.push(clone(messages.at(-1).data));
  actionResources.push({destroyed,timerDelays:[...timers.values()].map(timer=>timer.ms),calls:calls.length,messages:messages.length,
    listeners:[...listeners.values()].reduce((sum,group)=>sum+group.size,0),apiElements:hostDocument.head.children.length,
    saves:calls.filter(row=>row.url.endsWith('/save')).length});
}
const dom=node=>({tag:node.tagName,text:node.textContent,hidden:node.hidden,attributes:node.attributes,children:node.children.map(dom)});
process.stdout.write(JSON.stringify({calls,messages,scripts,configs:configs.map(config=>clone(config)),signed,order,attempts,timers:timers.size,actionStates,
  activeStatuses,maxActiveStatuses,beforeLate,destroyed,actionResources,signals:signals.map(signal=>signal.aborted),
  listeners:[...listeners.values()].reduce((sum,group)=>sum+group.size,0),apiElements:hostDocument.head.children.length,
  timerDelays:[...timers.values()].map(timer=>timer.ms),dom:dom(container)}));
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
    assert result["order"].index("listener:message") < result["order"].index("message:ocu:office-ready")
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


def test_publish_refused_during_own_autosave_retries_once_after_editing(tmp_path):
    result = _run(tmp_path, open=True, modify=[True], actions=[
        {"kind": "tick", "ms": 300000},
        {"kind": "command", "command": "save"},
        {"kind": "status", "status": {"state": "editing", "last_committed_seq": 3}},
        {"kind": "tick", "ms": 1000},
        {"kind": "status", "status": {
            "state": "editing", "save_seq": 4, "last_committed_seq": 4, "last_published_seq": 4,
        }},
        {"kind": "tick", "ms": 5000},
    ])
    saves = [row["body"]["intent"] for row in result["calls"] if row["url"].endswith("/save")]
    assert saves == ["persist", "publish", "publish"]
    assert result["actionStates"][1]["state"] == "saving"
    assert result["actionStates"][1]["reason"] is None
    assert _states(result)[-1]["state"] == "editing"
    assert _states(result)[-1]["dirty"] is False


def test_another_tabs_save_refusal_is_not_an_automatic_retry(tmp_path):
    result = _run(tmp_path, open=True, joined=True, status={"state": "saving"}, actions=[
        {"kind": "command", "command": "save"},
        {"kind": "status", "status": {"state": "editing"}},
        {"kind": "tick", "ms": 5000},
    ])
    assert len([row for row in result["calls"] if row["url"].endswith("/save")]) == 1
    assert _states(result)[-1]["state"] == "editing"
    assert _states(result)[-1]["reason"] == "session_not_editing"


def test_close_releases_editor_only_after_acceptance_and_polls_to_closed(tmp_path):
    result = _run(tmp_path, open=True, modify=[True], deferClose=True, actions=[
        {"kind": "command", "command": "close"},
        {"kind": "tick", "ms": 300000},
        {"kind": "releaseClose"},
        {"kind": "status", "status": {"state": "closed"}},
        {"kind": "tick", "ms": 1000},
        {"kind": "command", "command": "save"},
        {"kind": "command", "command": "close"},
        {"kind": "tick", "ms": 300000},
    ])
    assert len([row for row in result["calls"] if row["url"].endswith("/close")]) == 1
    assert result["actionResources"][0]["destroyed"] == 0
    assert 300000 not in result["actionResources"][0]["timerDelays"]
    assert result["actionResources"][1]["destroyed"] == 0
    assert result["actionResources"][2]["destroyed"] == 1
    assert result["actionStates"][2]["state"] == "closing"
    assert _states(result)[-1]["state"] == "closed"
    assert _states(result)[-1]["dirty"] is False
    assert result["timers"] == 0
    assert result["actionResources"][-1]["calls"] == result["actionResources"][4]["calls"]
    assert all(not row["url"].endswith("/save") for row in result["calls"])


def test_rejected_close_retains_editor_and_resumes_editing_timer(tmp_path):
    result = _run(tmp_path, open=True, modify=[True], rejectClose=True, actions=[
        {"kind": "command", "command": "close"},
        {"kind": "tick", "ms": 3000},
        {"kind": "allowClose"},
        {"kind": "command", "command": "close"},
    ])
    assert len([row for row in result["calls"] if row["url"].endswith("/close")]) == 2
    assert result["actionStates"][1]["state"] == "editing"
    assert result["actionStates"][1]["dirty"] is True
    assert result["actionStates"][1]["reason"] == "storage_low"
    assert result["actionResources"][1]["destroyed"] == 0
    assert result["actionResources"][1]["timerDelays"].count(300000) == 1
    assert result["destroyed"] == 1
    assert _states(result)[-1]["state"] == "closing"
    assert _states(result)[-1]["reason"] is None


def test_retirement_aborts_creation_and_ignores_abort_insensitive_completion(tmp_path):
    result = _run(tmp_path, open=True, deferCreate=True, actions=[
        {"kind": "pagehide"}, {"kind": "releaseCreate"}, {"kind": "tick", "ms": 300000},
        {"kind": "command", "command": "save"}, {"kind": "pagehide"},
    ])
    assert result["attempts"] == 0
    assert result["scripts"] == []
    assert len(result["calls"]) == result["actionResources"][0]["calls"] == 1
    assert len(result["messages"]) == result["actionResources"][0]["messages"]
    assert result["timers"] == result["listeners"] == 0
    assert result["signals"] == [True]


@pytest.mark.parametrize("later_edit", [False, True])
@pytest.mark.parametrize("committed_sequence", [3, 4])
def test_timeout_does_not_revoke_later_cumulative_coverage(tmp_path, later_edit, committed_sequence):
    actions = [
        {"kind": "command", "command": "save"},
        {"kind": "snapshot", "status": {"state": "editing", "reason": "save_timeout"}},
    ]
    if later_edit:
        actions.append({"kind": "modify"})
    actions.append({"kind": "snapshot", "status": {
        "state": "editing", "reason": None, "save_seq": committed_sequence,
        "last_committed_seq": committed_sequence, "last_published_seq": committed_sequence,
    }})
    result = _run(tmp_path, open=True, modify=[True], actions=actions)
    assert result["actionStates"][1]["dirty"] is True
    assert result["actionStates"][-1]["dirty"] is later_edit


@pytest.mark.parametrize(("options", "before", "release", "had_editor"), [
    ({"holdStatusAfter": 0}, [], "releaseStatus", False),
    ({"deferSave": True}, [{"kind": "command", "command": "save"}], "releaseSave", True),
    ({"deferClose": True}, [{"kind": "command", "command": "close"}], "releaseClose", True),
    ({"api": "timeout"}, [], "lateApi", False),
    ({"deferBody": "create"}, [], "releaseBody", False),
    ({"deferBody": "status"}, [], "releaseBody", False),
    ({"deferBody": "save"}, [{"kind": "command", "command": "save"}], "releaseBody", True),
    ({"deferBody": "close"}, [{"kind": "command", "command": "close"}], "releaseBody", True),
])
def test_retirement_fences_every_deferred_boundary(tmp_path, options, before, release, had_editor):
    result = _run(tmp_path, open=True, **options, actions=before + [
        {"kind": "pagehide"}, {"kind": release}, {"kind": "tick", "ms": 300000},
        {"kind": "dispose"},
    ])
    retired = result["actionResources"][len(before)]
    assert result["attempts"] == int(had_editor)
    assert result["destroyed"] == int(had_editor)
    assert len(result["calls"]) == retired["calls"]
    assert len(result["messages"]) == retired["messages"]
    assert result["timers"] == result["listeners"] == result["apiElements"] == result["activeStatuses"] == 0
    assert result["signals"] and all(result["signals"])


def test_constructor_time_disposal_destroys_the_returned_editor_once(tmp_path):
    result = _run(tmp_path, open=True, syncDispose=True, actions=[
        {"kind": "modify"}, {"kind": "error"}, {"kind": "dispose"}, {"kind": "tick", "ms": 300000},
    ])
    assert result["attempts"] == result["destroyed"] == 1
    assert result["timers"] == result["listeners"] == result["apiElements"] == 0
    assert result["actionResources"][0]["messages"] == result["actionResources"][-1]["messages"]
    assert result["actionResources"][0]["calls"] == result["actionResources"][-1]["calls"]
    assert all(not row["url"].endswith("/close") for row in result["calls"])


def test_twenty_retired_hosts_release_editing_and_saving_resources(tmp_path):
    actions, retirements = [], []
    for index in range(20):
        if index % 2:
            actions += [{"kind": "modify"}, {"kind": "command", "command": "save"}]
        retirements.append(len(actions))
        actions.append({"kind": "dispose"})
        actions.append({"kind": "tick", "ms": 1000})
        if index != 19:
            actions.append({"kind": "remount"})
    result = _run(tmp_path, open=True, actions=actions)
    assert result["attempts"] == result["destroyed"] == 20
    for index in retirements:
        retired = result["actionResources"][index]
        assert retired["listeners"] == retired["apiElements"] == 0
        assert retired["timerDelays"] == []
        assert result["actionResources"][index + 1]["calls"] == retired["calls"]
        assert result["actionResources"][index + 1]["messages"] == retired["messages"]
    assert all(not row["url"].endswith("/close") for row in result["calls"])
    assert all(result["signals"])


def test_terminal_status_before_close_reply_still_releases_accepted_editor(tmp_path):
    result = _run(tmp_path, open=True, deferClose=True, actions=[
        {"kind": "command", "command": "close"},
        {"kind": "status", "status": {"state": "closed"}},
        {"kind": "tick", "ms": 1000},
        {"kind": "releaseClose"},
    ])
    assert result["actionResources"][2]["destroyed"] == 0
    assert result["destroyed"] == 1
    assert result["actionStates"][-1]["state"] == "closed"
    assert result["actionResources"][-1]["calls"] == result["actionResources"][2]["calls"]
    assert result["actionResources"][-1]["messages"] == result["actionResources"][2]["messages"]


@pytest.mark.parametrize("first_intent", ["publish", "persist"])
def test_unchanged_publishing_save_finishes_clean_without_inventing_edits(tmp_path, first_intent):
    first = {"kind": "command", "command": "save"} if first_intent == "publish" else {"kind": "tick", "ms": 300000}
    result = _run(tmp_path, prefix="/ocu", open=True, modify=[True], actions=[
        first, {"kind": "status", "status": {
            "state": "editing", "last_committed_seq": 3, "last_published_seq": 3 if first_intent == "publish" else 2,
        }}, {"kind": "tick", "ms": 1000}, {"kind": "command", "command": "save"},
        {"kind": "status", "status": {"state": "editing", "last_committed_seq": 4, "last_published_seq": 4}},
        {"kind": "tick", "ms": 1000}, {"kind": "command", "command": "close"},
    ])
    saves = [row["body"]["intent"] for row in result["calls"] if row["url"].endswith("/save")]
    assert saves == [first_intent, "publish"]
    assert result["actionStates"][5]["dirty"] is False
    if first_intent == "publish":
        assert all(state["dirty"] is False for state in result["actionStates"][3:])
    assert all(row["url"].startswith(f"/ocu/api/office/{CHAT}/") for row in result["calls"])
    assert all(row["url"].count("/ocu/") == 1 for row in result["calls"])
    assert all(row["headers"]["x-requested-with"] == "ocu-workspace" for row in result["calls"])


@pytest.mark.parametrize("transport", [False, True])
def test_rejected_publishing_save_stays_dirty_and_retryable(tmp_path, transport):
    result = _run(tmp_path, open=True, modify=[True], rejectSave=not transport, saveTransportError=transport, actions=[
        {"kind": "command", "command": "save"}, {"kind": "tick", "ms": 3000},
        {"kind": "allowSave"}, {"kind": "command", "command": "save"},
    ])
    failed = result["actionStates"][1]
    assert failed["state"] == "editing" and failed["dirty"] is True and failed["reason"]
    if not transport:
        assert failed["reason"] == "documentserver_unavailable"
    assert result["actionResources"][1]["timerDelays"].count(300000) == 1
    assert _states(result)[-1]["state"] == "saving" and _states(result)[-1]["dirty"] is True
    assert _states(result)[-1]["reason"] is None
    assert len([row for row in result["calls"] if row["url"].endswith("/save")]) == 2


@pytest.mark.parametrize("reason", ["save_timeout", "storage_low"])
def test_callback_failure_keeps_save_and_autosave_usable(tmp_path, reason):
    result = _run(tmp_path, open=True, modify=[True], actions=[
        {"kind": "command", "command": "save"},
        {"kind": "status", "status": {"state": "editing", "reason": reason}},
        {"kind": "tick", "ms": 3000}, {"kind": "command", "command": "save"},
        {"kind": "status", "status": {"state": "editing", "reason": reason}},
        {"kind": "tick", "ms": 301000},
    ])
    assert result["actionStates"][2]["state"] == "editing"
    assert result["actionStates"][2]["dirty"] is True and result["actionStates"][2]["reason"] == reason
    assert result["actionResources"][2]["timerDelays"].count(300000) == 1
    assert [row["body"]["intent"] for row in result["calls"] if row["url"].endswith("/save")] == ["publish", "publish", "persist"]
    assert _states(result)[-1]["dirty"] is True


@pytest.mark.parametrize("state", ["closed", "error", "orphaned"])
def test_observed_final_state_stops_live_host_work(tmp_path, state):
    result = _run(tmp_path, open=True, modify=[True], actions=[
        {"kind": "status", "status": {"state": state, "reason": "final_failure" if state == "error" else None}},
        {"kind": "tick", "ms": 1000}, {"kind": "command", "command": "save"},
        {"kind": "command", "command": "close"}, {"kind": "modify"}, {"kind": "tick", "ms": 300000},
    ])
    assert _states(result)[-1]["state"] == state
    assert _states(result)[-1]["dirty"] is (state != "closed")
    assert result["timerDelays"] == []
    assert result["actionResources"][-1]["calls"] == result["actionResources"][1]["calls"]
    assert result["actionResources"][-1]["messages"] == result["actionResources"][1]["messages"]


def test_old_status_cannot_reverse_a_later_save_mutation(tmp_path):
    result = _run(tmp_path, open=True, modify=[True], status={"state": "saving"}, holdStatusAfter=1, actions=[
        {"kind": "status", "status": {"state": "editing"}}, {"kind": "tick", "ms": 1000},
        {"kind": "command", "command": "save"}, {"kind": "allowStatus"}, {"kind": "releaseStatus"},
    ])
    assert result["maxActiveStatuses"] == 1
    assert _states(result)[-1]["state"] == "saving" and _states(result)[-1]["dirty"] is True
    assert all(state["state"] != "editing" for state in _states(result))


@pytest.mark.parametrize("variant", ["sibling", "nested", "origin", "chat", "generation", "extra", "missing", "unknown"])
def test_bound_host_rejects_unauthorized_and_malformed_commands(tmp_path, variant):
    data = {"type": "ocu:office-command", "chat_id": CHAT, "generation": 7, "command": "save"}
    action = {"kind": "message", "data": data}
    if variant in {"sibling", "nested"}:
        action["source"] = variant
    elif variant == "origin":
        action["origin"] = "https://foreign.test"
    elif variant == "chat":
        data["chat_id"] = "another-chat"
    elif variant == "generation":
        data["generation"] = 6
    elif variant == "extra":
        data.update(command="close", extra=True)
    elif variant == "missing":
        del data["generation"]
    else:
        data["command"] = "unknown"
    result = _run(tmp_path, open=True, actions=[action])
    assert [row["method"] for row in result["calls"]] == ["POST", "GET"]
    assert _states(result)[-1]["state"] == "editing" and _states(result)[-1]["dirty"] is False


def test_close_supersedes_an_already_queued_publishing_retry(tmp_path):
    result = _run(tmp_path, open=True, modify=[True], actions=[
        {"kind": "tick", "ms": 300000}, {"kind": "command", "command": "save"},
        {"kind": "command", "command": "close"},
        {"kind": "status", "status": {"state": "closed"}}, {"kind": "tick", "ms": 300000},
    ])
    assert [row["body"]["intent"] for row in result["calls"] if row["url"].endswith("/save")] == ["persist", "publish"]
    assert len([row for row in result["calls"] if row["url"].endswith("/close")]) == 1
    assert _states(result)[-1]["state"] == "closed" and result["destroyed"] == 1
    assert result["timers"] == 0


def test_queued_publish_waits_for_owned_autosave_acceptance_before_retry(tmp_path):
    result = _run(tmp_path, open=True, modify=[True], deferSave=True, actions=[
        {"kind": "tick", "ms": 300000}, {"kind": "command", "command": "save"},
        {"kind": "status", "status": {"state": "editing", "last_committed_seq": 3}},
        {"kind": "tick", "ms": 1000}, {"kind": "releaseSave"}, {"kind": "releaseSave"},
        {"kind": "status", "status": {
            "state": "editing", "save_seq": 4, "last_committed_seq": 4, "last_published_seq": 4,
        }}, {"kind": "tick", "ms": 1000},
    ])
    assert result["actionResources"][3]["saves"] == 2
    assert result["actionResources"][4]["saves"] == 3
    assert all(state["reason"] is None for state in result["actionStates"])
    assert _states(result)[-1]["state"] == "editing" and _states(result)[-1]["dirty"] is False


def test_accepted_autosave_owns_retry_before_its_status_is_observed(tmp_path):
    result = _run(tmp_path, open=True, modify=[True], holdStatusAfter=300, actions=[
        {"kind": "tick", "ms": 300000},
        {"kind": "command", "command": "save"},
        {"kind": "allowStatus"}, {"kind": "releaseStatus"},
        {"kind": "status", "status": {"state": "editing", "last_committed_seq": 3}},
        {"kind": "tick", "ms": 1000},
        {"kind": "status", "status": {
            "state": "editing", "save_seq": 4, "last_committed_seq": 4, "last_published_seq": 4,
        }},
        {"kind": "tick", "ms": 5000},
    ])
    assert result["actionStates"][0]["state"] == "editing"
    assert [row["body"]["intent"] for row in result["calls"] if row["url"].endswith("/save")] == [
        "persist", "publish", "publish",
    ]
    assert all(state["reason"] is None for state in result["actionStates"])
    assert _states(result)[-1]["state"] == "editing" and _states(result)[-1]["dirty"] is False


def test_failed_owned_autosave_keeps_queued_publication_without_covering_edits(tmp_path):
    result = _run(tmp_path, open=True, modify=[True], deferPersistFailure=True, actions=[
        {"kind": "tick", "ms": 300000}, {"kind": "command", "command": "save"},
        {"kind": "status", "status": {"last_committed_seq": 3, "last_published_seq": 3}},
        {"kind": "releaseSave"}, {"kind": "tick", "ms": 5000},
        {"kind": "status", "status": {
            "state": "editing", "save_seq": 4, "last_committed_seq": 4, "last_published_seq": 4,
        }},
        {"kind": "tick", "ms": 5000},
    ])
    assert [row["body"]["intent"] for row in result["calls"] if row["url"].endswith("/save")] == [
        "persist", "publish", "publish",
    ]
    assert all(state["dirty"] for state in result["actionStates"][:5])
    assert all(state["reason"] is None for state in result["actionStates"])
    assert _states(result)[-1]["state"] == "editing" and _states(result)[-1]["dirty"] is False


@pytest.mark.parametrize("command", ["save", "close"])
@pytest.mark.parametrize(("state", "reason"), [
    ("conflict", "path_missing"), ("error", "state_corrupt"), ("orphaned", "editor_state_lost"),
])
def test_broker_failure_reason_overrides_prior_command_failure(tmp_path, command, state, reason):
    result = _run(tmp_path, open=True, modify=[True], rejectSave=True, rejectClose=True, actions=[
        {"kind": "command", "command": command}, {"kind": "tick", "ms": 1000},
        {"kind": "status", "status": {"state": state, "reason": reason}},
        {"kind": "tick", "ms": 1000},
    ])
    failed_command_reason = "documentserver_unavailable" if command == "save" else "storage_low"
    assert result["actionStates"][1]["state"] == "editing"
    assert result["actionStates"][1]["reason"] == failed_command_reason
    assert _states(result)[-1]["state"] == state
    assert _states(result)[-1]["reason"] == reason
    assert _states(result)[-1]["dirty"] is True


def test_pending_but_refused_autosave_cannot_own_another_tabs_retry(tmp_path):
    result = _run(tmp_path, open=True, modify=[True], deferPersistRefusal=True, actions=[
        {"kind": "tick", "ms": 299999},
        {"kind": "status", "status": {"state": "saving", "save_seq": 3}},
        {"kind": "tick", "ms": 1}, {"kind": "command", "command": "save"},
        {"kind": "status", "status": {"state": "editing"}},
        {"kind": "releaseSave"}, {"kind": "tick", "ms": 5000},
    ])
    assert [row["body"]["intent"] for row in result["calls"] if row["url"].endswith("/save")] == ["persist", "publish"]
    assert _states(result)[-1]["reason"] == "session_not_editing"
    assert _states(result)[-1]["dirty"] is True


@pytest.mark.parametrize("ending", ["close", "dispose"])
def test_failed_autosave_cannot_revive_a_superseded_publication(tmp_path, ending):
    result = _run(tmp_path, open=True, modify=[True], deferPersistFailure=True, actions=[
        {"kind": "tick", "ms": 300000}, {"kind": "command", "command": "save"},
        {"kind": "command", "command": "close"} if ending == "close" else {"kind": "dispose"},
        {"kind": "releaseSave"}, {"kind": "tick", "ms": 5000},
    ])
    assert [row["body"]["intent"] for row in result["calls"] if row["url"].endswith("/save")] == ["persist", "publish"]
    assert result["destroyed"] == 1
    if ending == "dispose":
        assert result["actionResources"][2]["messages"] == result["actionResources"][-1]["messages"]
        assert result["timers"] == result["listeners"] == 0


@pytest.mark.parametrize("timed_out", [False, True])
def test_old_autosave_does_not_own_a_later_foreign_allocation(tmp_path, timed_out):
    transition = [
        {"kind": "status", "status": {"state": "editing", "reason": "save_timeout"}},
        {"kind": "tick", "ms": 1000},
    ] if timed_out else []
    result = _run(tmp_path, open=True, modify=[True], actions=[
        {"kind": "tick", "ms": 300000}, *transition,
        {"kind": "status", "status": {"state": "saving", "save_seq": 4, "reason": None}},
        {"kind": "tick", "ms": 1000}, {"kind": "command", "command": "save"},
        {"kind": "status", "status": {"state": "editing"}},
        {"kind": "tick", "ms": 5000},
    ])
    assert [row["body"]["intent"] for row in result["calls"] if row["url"].endswith("/save")] == ["persist", "publish"]
    assert _states(result)[-1]["reason"] == "session_not_editing"
    assert _states(result)[-1]["dirty"] is True

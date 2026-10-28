import assert from 'node:assert/strict'
import {readFile} from 'node:fs/promises'
const source=await readFile(new URL('../webui/static/research-evidence.js',import.meta.url),'utf8')
const {editorState,selectDraft,isSaved,hasChanges,resetEditor,claimInputs,removePassage,saveDraft,lockDraft,claimStatus}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'))
const raw={text:'收入增长不等于股价上涨',expires_at:'2026-11-01T00:00:00+08:00',passages:[{source_id:'synthetic',locator:'p1',quote:'本期 1200，上期 1000',published_at:'2026-10-01T08:00:00+08:00',first_seen_at:'2026-10-01T09:00:00+08:00',unit:'CNY_100M',currency:'CNY',basis:'consolidated',period_kind:'annual'}],calculation:{operation:'growth_pct',expected:'20',operands:[{passage_index:0,value:'1200'},{passage_index:0,value:'1000'}]},verification:{entailment:'supported'},computed_value:'999'}
const draft={object_id:'600001',text:'合成研究',revision:3,claims:[raw]}
const e=editorState()
selectDraft(e,draft)
assert(isSaved(e));assert(!hasChanges(e))
e.claims[0].passages[0].quote='改动'
assert.equal(draft.claims[0].passages[0].quote,raw.passages[0].quote)
assert(!isSaved(e));assert(hasChanges(e))
let calls=0
await lockDraft(e,async()=>{calls++})
assert.equal(calls,0);assert.match(e.message,/请先保存/)
resetEditor(e);assert(isSaved(e))
assert.equal(claimInputs([raw])[0].verification,undefined)
assert.equal(claimInputs([raw])[0].computed_value,undefined)

// Referenced citations cannot disappear or silently point to the next quote.
const c=claimInputs([raw])[0]
assert.equal(removePassage(c,0),false)
c.passages.unshift({source_id:'unused'})
c.calculation.operands.forEach(o=>o.passage_index++)
assert.equal(removePassage(c,0),true)
assert.deepEqual(c.calculation.operands.map(o=>o.passage_index),[0,0])

// Canonical server timestamps do not make an untouched save look dirty.
let submitted
const saved=await saveDraft(e,async(url,payload)=>{
  assert.equal(url,'/api/research/thesis/draft');submitted=payload
  const canonical=claimInputs(payload.claims)
  canonical[0].passages[0].published_at='2026-10-01T00:00:00+00:00'
  return {...draft,claims:canonical,revision:4}
})
assert.equal(submitted.expected_revision,3)
assert.equal(saved.revision,4);assert(isSaved(e));assert(!e.busy)
await lockDraft(e,async(url,payload)=>{
  assert.equal(url,'/api/research/thesis/lock')
  assert.deepEqual(payload,{symbol:'600001',draft_revision:4,confirm:true});calls++
  return {status:'locked'}
})
assert.equal(calls,1)

// Network failure and CAS conflicts retain every unsaved field and saved revision.
e.claims[0].passages[0].quote='未保存原文'
const before=JSON.stringify(e.claims)
await saveDraft(e,async()=>{throw Error('revision_conflict')})
assert.equal(JSON.stringify(e.claims),before);assert.equal(e.saved.revision,4)
assert.match(e.message,/最新版本/);assert(!e.busy)

// Duplicate submission is suppressed; late replies never erase concurrent edits.
let resolve
const pending=saveDraft(e,(_,p)=>new Promise(r=>{resolve=()=>r({...draft,claims:p.claims,revision:5})}))
await saveDraft(e,()=>{throw Error('must not submit twice')})
e.text='等待期间的新文字'
resolve();await pending
assert.equal(e.text,'等待期间的新文字');assert.equal(e.saved.revision,5)
assert(!isSaved(e));assert.match(e.message,/未保存修改/)
const foreign=editorState();foreign.symbol='600002';foreign.text='新股票'
await saveDraft(foreign,async(_,p)=>{assert.equal(p.expected_revision,0);assert.deepEqual(p.claims,[]);throw Error('offline')})
assert.equal(foreign.symbol,'600002');assert.equal(foreign.saved,null)
assert.match(claimStatus({arithmetic:'verified',computed_value:'20',temporal:'eligible'}),/文字结论待核实/)
assert.match(claimStatus({arithmetic:'not_verified',temporal:'invalid',reasons:['post_decision_evidence_forbidden']}),/晚于研究时点/)

// Actual API helper preserves FastAPI's reason code for inline recovery guidance.
let lib=await readFile(new URL('../webui/static/lib.js',import.meta.url),'utf8')
lib=lib.replace("import { reactive, computed } from 'vue'",'const reactive=x=>x;const computed=f=>({value:f()})')
globalThis.window={addEventListener(){}}
const {api}=await import('data:text/javascript;base64,'+Buffer.from(lib).toString('base64'))
globalThis.fetch=async()=>({status:422,json:async()=>({detail:'post_decision_evidence_forbidden'})})
await assert.rejects(api('/api/research/thesis/draft'),/post_decision_evidence_forbidden/)
globalThis.fetch=async()=>({status:200,json:async()=>({ok:true,data:{revision:5}})})
assert.deepEqual(await api('/api/research/cases'),{revision:5})
console.log('research evidence UI: editing, provenance, CAS, races, API errors passed')

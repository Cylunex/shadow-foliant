// Editing state contains original inputs only. Verification always comes from the server.
export const OPERATIONS = {sum:'求和', difference:'相减（第一项 − 第二项）', ratio:'比值（第一项 ÷ 第二项）', growth_pct:'增长率（第一项 ÷ 第二项 − 1）× 100%'}
export const REASONS = {
  post_decision_evidence_forbidden:'首次观测晚于研究时点，请核对时间', passage_observed_before_publication:'首次观测不能早于发布时间',
  evidence_cutoff_unknown:'历史证据截止时点缺失', operand_not_in_passage:'引用原文中找不到完整数值',
  incompatible_calculation_semantics:'计算的单位、币种、口径或期间类型不一致', calculation_semantics_required:'请补齐参与计算的引用口径',
  claim_number_mismatch:'声明结果与重算不符', claim_expired_or_expiry_invalid:'引用已过期或有效期不合法',
  passage_provenance_required:'请补齐来源、定位、原文和两个时间', unsupported_calculation:'算式不支持、项数不符或分母为零',
  timezone_required:'时间必须包含时区，如 +08:00', timestamp_required:'请填写完整时间', timestamp_invalid:'时间格式不合法',
  invalid_numeric_literal:'数字表达不合法', numeric_literal_too_large:'数字过长', invalid_thesis:'请填写六位代码和论点文字',
  claim_requires_bounded_evidence:'每条论断需有文字和 1–12 段引用', bounded_operands_required:'算式需有 1–12 个数值',
  operand_source_required:'请选择数值所在的引用', revision_conflict:'草稿已被更新，请读取最新版本后合并修改',
  stale_thesis_draft:'草稿版本已变化，请读取最新草稿', passage_too_large:'单项引用内容不能超过 4000 字',
}
export const evidenceError = error => REASONS[error?.message] || String(error)
export const blankPassage = () => ({source_id:'', locator:'', quote:'', published_at:'', first_seen_at:'', unit:'', currency:'', basis:'', period_kind:''})
export const blankClaim = () => ({text:'', expires_at:'', passages:[blankPassage()], calculation:null})
export function claimInputs(claims = []) {
  return JSON.parse(JSON.stringify(claims.map(c => ({text:c?.text ?? '', expires_at:c?.expires_at ?? '', passages:c?.passages ?? [], calculation:c?.calculation ?? null}))))
}
export function removePassage(claim, index) {
  if ((claim.calculation?.operands || []).some(o => o.passage_index === index)) return false
  claim.passages.splice(index, 1)
  for (const operand of claim.calculation?.operands || []) if (operand.passage_index > index) operand.passage_index--
  return true
}
export function claimStatus(c) {
  const arithmetic=c.arithmetic==='verified'?`数字已重算：${c.computed_value}`:c.arithmetic==='not_requested'?'未提供算式':'数字尚未通过核对'
  const time=c.temporal==='eligible'?'引用时间符合研究时点':c.temporal==='invalid'?'引用时间越界':'引用时点待核实'
  const detail=(c.reasons||[]).filter(r=>!['source_authenticity_unverified','semantic_entailment_unverified'].includes(r)).map(r=>REASONS[r]||'引用字段需复核').join('；')
  return `${arithmetic} · ${time} · 文字结论待核实${detail?'；'+detail:''}`
}

export function editorState() { return {symbol:'', text:'', claims:[], saved:null, busy:false, message:''} }
export function selectDraft(editor, draft) {
  editor.symbol=draft.object_id; editor.text=draft.text; editor.claims=claimInputs(draft.claims)
  editor.saved=JSON.parse(JSON.stringify(draft)); editor.message=''
}
export function isSaved(editor) {
  return !!editor.saved && editor.symbol===editor.saved.object_id && editor.text===editor.saved.text &&
    JSON.stringify(claimInputs(editor.claims))===JSON.stringify(claimInputs(editor.saved.claims))
}
export function hasChanges(editor) {
  return editor.saved ? !isSaved(editor) : !!(editor.symbol || editor.text || editor.claims.length)
}
export function resetEditor(editor) {
  if(editor.saved) selectDraft(editor,editor.saved)
  else Object.assign(editor,editorState())
}
export async function saveDraft(editor, post) {
  if(editor.busy) return
  editor.busy=true; editor.message=''
  // Snapshot the submitted revision; keep in-flight edits instead of overwriting them.
  const payload={symbol:editor.symbol, text:editor.text, claims:claimInputs(editor.claims), expected_revision:editor.saved?.object_id===editor.symbol?editor.saved.revision:0}
  try {
    const result=await post('/api/research/thesis/draft',payload)
    if(editor.symbol===payload.symbol) {
      const unchanged=editor.text===payload.text && JSON.stringify(claimInputs(editor.claims))===JSON.stringify(payload.claims)
      editor.saved=result
      if(unchanged) editor.claims=claimInputs(result.claims)
    }
    editor.message=isSaved(editor)?'草稿与证据已保存并核验，尚未锁定':'提交版本已保存；当前还有未保存修改'
    return result
  } catch(error) { editor.message=evidenceError(error) }
  finally { editor.busy=false }
}
export async function lockDraft(editor, post) {
  if(editor.busy) return
  if(!isSaved(editor)){editor.message='内容或证据已变更，请先保存新草稿';return}
  editor.busy=true
  try {
    const result=await post('/api/research/thesis/lock',{symbol:editor.saved.object_id,draft_revision:editor.saved.revision,confirm:true})
    editor.message='已锁定当前版本；文字结论仍待核实'
    return result
  } catch(error) { editor.message=evidenceError(error) }
  finally { editor.busy=false }
}

export const EvidenceDetails = {
  props:['thesis'],
  setup(){return {claimStatus,operations:OPERATIONS}},
  template:`<div class="research-evidence">
    <p class="list-meta">证据截止 {{thesis.evidence_as_of || thesis.created_at || '历史记录未提供'}} · 核验结果不认证来源真实性或文字因果。</p>
    <p v-if="!thesis.claims?.length" class="notice">尚未附引用，文字结论待核实。</p>
    <details v-for="(claim,i) in thesis.claims||[]" :key="i" class="evidence-claim">
      <summary>论断 {{i+1}}：{{claim?.text || '历史论断缺少文字'}}</summary>
      <p>{{claimStatus(thesis.validity?.claim_checks?.find(c=>c.index===i)||{})}}</p>
      <p class="list-meta">有效期至 {{claim?.expires_at || '未提供'}}</p>
      <div v-for="(p,j) in claim?.passages||[]" :key="j" class="evidence-passage">
        <b>引用 {{j+1}} · {{p.source_id}}</b><p>定位：{{p.locator}}</p>
        <blockquote>{{p.quote || '原文缺失'}}</blockquote>
        <p class="list-meta">发布 {{p.published_at}}<br>首次观测 {{p.first_seen_at}}</p>
        <p class="list-meta">单位 {{p.unit||'未提供'}} · 币种 {{p.currency||'未提供'}} · 口径 {{p.basis||'未提供'}} · 期间类型 {{p.period_kind||'未提供'}}</p>
      </div>
      <div v-if="claim?.calculation" class="notice">
        <b>{{operations[claim.calculation.operation] || '未知算式'}}</b>
        <p v-for="(o,j) in claim.calculation.operands||[]" :key="j">第 {{j+1}} 项：{{o.value}} ← 引用 {{Number(o.passage_index)+1}}</p>
        <p>声明结果 {{claim.calculation.expected ?? '未指定'}} · 服务端重算 {{thesis.validity?.claim_checks?.find(c=>c.index===i)?.computed_value ?? '未通过'}}</p>
      </div>
    </details>
  </div>`
}

export const EvidenceEditor = {
  props:['claims','disabled'],
  setup(){return {blankClaim,blankPassage,removePassage,operations:OPERATIONS}},
  template:`<fieldset class="evidence-editor" :disabled="disabled">
    <legend>引用与数字核算</legend>
    <p class="list-meta">时间填写 ISO 格式并保留时区，例如 2026-10-01T09:00:00+08:00。发布时间、首次观测和有效期分别填写；研究截止时点由保存时的服务端记录。</p>
    <p v-if="!claims.length">可先保存文字草稿；补齐引用后再复核。</p>
    <details v-for="(c,i) in claims" :key="i" class="evidence-claim" open>
      <summary>编辑论断 {{i+1}}：{{c.text || '待填写'}}</summary>
      <label>论断文字<textarea v-model="c.text" rows="2" maxlength="4000"></textarea></label>
      <label>引用有效期至<input v-model="c.expires_at" placeholder="YYYY-MM-DDTHH:mm:ss+08:00"></label>
      <div v-for="(p,j) in c.passages" :key="j" class="evidence-passage">
        <b>引用 {{j+1}}</b><div class="evidence-fields">
          <label>来源标识<input v-model="p.source_id" maxlength="4000" placeholder="报告名称或来源 ID"></label>
          <label>原文定位<input v-model="p.locator" maxlength="4000" placeholder="页码 / 表格 / 段落"></label>
          <label>发布时间<input v-model="p.published_at" placeholder="YYYY-MM-DDTHH:mm:ss+08:00"></label>
          <label>首次观测时间<input v-model="p.first_seen_at" placeholder="YYYY-MM-DDTHH:mm:ss+08:00"></label>
        </div>
        <label>引用原文<textarea v-model="p.quote" rows="3" maxlength="4000" placeholder="保留完整数字与上下文，不自动抓取外部链接"></textarea></label>
        <div class="evidence-fields">
          <label>单位<input v-model="p.unit" maxlength="100" placeholder="如 CNY_100M"></label>
          <label>币种<input v-model="p.currency" maxlength="100" placeholder="如 CNY"></label>
          <label>统计口径<input v-model="p.basis" maxlength="100" placeholder="如 consolidated"></label>
          <label>期间类型<input v-model="p.period_kind" maxlength="100" placeholder="如 annual"></label>
        </div>
        <button type="button" class="ghost" :disabled="c.passages.length<=1 || c.calculation?.operands?.some(o=>o.passage_index===j)" @click="removePassage(c,j)">移除引用 {{j+1}}</button>
        <small>参与算式的引用需先调整数值来源，才能移除。</small>
      </div>
      <button type="button" class="ghost" :disabled="c.passages.length>=12" @click="c.passages.push(blankPassage())">添加引用</button>
      <label>核算方式<select aria-label="核算方式" :value="c.calculation?.operation||''" @change="c.calculation=$event.target.value?{...c.calculation,operation:$event.target.value,operands:c.calculation?.operands||[{passage_index:0,value:''},{passage_index:0,value:''}]}:null">
        <option value="">仅保留引用，不计算</option><option v-for="(label,key) in operations" :key="key" :value="key">{{label}}</option>
      </select></label>
      <template v-if="c.calculation">
        <div v-for="(o,j) in c.calculation.operands" :key="j" class="evidence-fields">
          <label>第 {{j+1}} 项数值<input v-model="o.value" inputmode="decimal" maxlength="64" placeholder="原文完整数值"></label>
          <label>数值来源<select v-model="o.passage_index"><option v-for="(p,k) in c.passages" :key="k" :value="k">引用 {{k+1}} · {{p.source_id || '待填写'}}</option></select></label>
          <button type="button" class="ghost" :disabled="c.calculation.operands.length<=1" @click="c.calculation.operands.splice(j,1)">移除第 {{j+1}} 项</button>
        </div>
        <button type="button" class="ghost" :disabled="c.calculation.operands.length>=12" @click="c.calculation.operands.push({passage_index:0,value:''})">添加数值</button>
        <label v-if="c.calculation.expected!==undefined">声明结果（已有值）<input v-model="c.calculation.expected" maxlength="64"></label>
        <p class="list-meta">按显示顺序计算；比值、相减和增长率需要两项。数值必须出现在所选原文中，参与计算的口径必须一致。保存后显示服务端重算值。</p>
      </template>
      <button type="button" class="ghost" @click="claims.splice(i,1)">移除论断 {{i+1}}</button>
    </details>
    <button type="button" class="ghost" :disabled="claims.length>=20" @click="claims.push(blankClaim())">添加论断与引用</button>
  </fieldset>`
}

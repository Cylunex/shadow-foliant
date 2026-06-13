import { reactive, ref, computed, onMounted } from 'vue'
import { api, mdLite } from '../lib.js'

const SCOPES = [
  { k:'stock', t:'个股(需代码)' }, { k:'market', t:'大盘/市场' },
  { k:'portfolio', t:'持仓' }, { k:'fund', t:'基金(需代码)' },
]

export default {
  template: `
  <div>
    <div class="h1">🧩 AI 工作流</div>
    <p class="sub">选数据 → 配智能体(提示词/模型) → 两层流程(并行分析→综合) → 出结果。存成模板复用。</p>

    <div class="card">
      <div class="row">
        <div style="flex:1;min-width:220px"><label>已存工作流</label>
          <select v-model="curId" @change="loadSel" style="width:100%">
            <option :value="null">— 新建 —</option>
            <option v-for="w in saved" :key="w.id" :value="w.id">{{w.name}}</option>
          </select>
        </div>
        <button class="ghost" @click="newWf">+ 新建</button>
        <button class="ghost" :disabled="!curId" @click="saveAsNew">另存为</button>
        <button class="ghost" :disabled="!curId" @click="delWf">删除</button>
        <button :disabled="busy.save" @click="saveWf">{{busy.save?'保存中…':'💾 保存'}}</button>
      </div>
    </div>

    <div class="card">
      <h3>① 基本</h3>
      <div class="row">
        <div style="flex:2;min-width:220px"><label>名称</label><input v-model="wf.name" placeholder="如 个股双层研判" style="width:100%"/></div>
        <div><label>类型(scope)</label><select v-model="wf.scope"><option v-for="s in scopes" :value="s.k">{{s.t}}</option></select></div>
        <div v-if="needCode"><label>运行参数:代码</label><input v-model="run.code" placeholder="如 600519" style="width:130px"/></div>
        <div v-if="wf.scope==='stock'"><label>(可选)RAG查询</label><input v-model="run.query" placeholder="留空用代码" style="width:150px"/></div>
      </div>
    </div>

    <div class="card">
      <h3>② 数据块 <span style="color:var(--muted);font-weight:400;font-size:12px">勾选要喂给 AI 的数据(按类型过滤)</span>
        <button class="ghost" style="float:right" :disabled="!wf.data.length||prev.loading" @click="previewData">{{prev.loading?'取数中…':'👁 预览所选数据'}}</button></h3>
      <div style="display:flex;flex-wrap:wrap;gap:6px 16px">
        <label v-for="p in filteredProviders" :key="p.key" style="display:flex;align-items:center;gap:5px;cursor:pointer;font-weight:400">
          <input type="checkbox" :value="p.key" v-model="wf.data"/> {{p.name}} <span style="color:var(--muted);font-size:11px">{{p.key}}</span>
        </label>
      </div>
      <div v-if="prev.data" style="margin-top:10px">
        <div v-for="(v,k) in prev.data" :key="k" style="margin-bottom:6px">
          <b style="font-size:12px">{{provName(k)}} <span style="color:var(--muted)">{{phData(k)}}</span></b>
          <pre style="white-space:pre-wrap;color:var(--muted);margin:2px 0 0;font:11px/1.5 inherit;max-height:130px;overflow:auto;background:var(--panel2);padding:8px;border-radius:7px">{{prevStr(v)}}</pre>
        </div>
      </div>
    </div>

    <div class="card">
      <h3>③ 分析师(第一层,并行) <button class="ghost" style="float:right" @click="addAnalyst">+ 加分析师</button></h3>
      <div v-for="(a,i) in wf.analysts" :key="i" style="border:1px solid var(--line);border-radius:9px;padding:12px;margin-bottom:10px">
        <div class="row">
          <div><label>名称</label><input v-model="a.name" style="width:130px"/></div>
          <div><label>模型(可选)</label><input v-model="a.model" placeholder="默认路由" style="width:140px"/></div>
          <div style="flex:1;min-width:180px"><label>看哪些数据(留空=全部)</label>
            <select multiple v-model="a.inputs" style="width:100%;height:54px">
              <option v-for="k in wf.data" :key="k" :value="k">{{provName(k)}}</option>
            </select>
          </div>
          <button class="ghost" @click="wf.analysts.splice(i,1)" style="align-self:flex-end">删</button>
        </div>
        <label style="margin-top:8px;display:block">System(角色设定)</label>
        <textarea v-model="a.system" rows="1" placeholder="你是A股技术分析师" style="width:100%"></textarea>
        <label style="margin-top:6px;display:block">User 提示词 · 占位符写法:<code>{{ph.code}}</code> 个股代码、<code>{{ph.dataEx}}</code> 数据块(key 见上方数据块)</label>
        <textarea v-model="a.user" rows="3" placeholder="在此写提示词,用占位符引用数据(语法见上方标签)" style="width:100%"></textarea>
      </div>
      <div v-if="!wf.analysts.length" class="loading">还没有分析师,点"+加分析师"。</div>
    </div>

    <div class="card">
      <h3>④ 综合(第二层) <span style="color:var(--muted);font-weight:400;font-size:12px">汇总各分析师输出,出最终结论</span></h3>
      <div class="row"><div><label>名称</label><input v-model="wf.synthesizer.name" style="width:130px"/></div>
        <div><label>模型(可选)</label><input v-model="wf.synthesizer.model" placeholder="默认路由" style="width:140px"/></div></div>
      <label style="margin-top:8px;display:block">System</label>
      <textarea v-model="wf.synthesizer.system" rows="1" placeholder="你是首席策略师" style="width:100%"></textarea>
      <label style="margin-top:6px;display:block">User · 用 <code>{{ph.analystEx}}</code> 引用上层各分析师的输出</label>
      <textarea v-model="wf.synthesizer.user" rows="3" placeholder="综合上层各分析师意见,给评级/目标/止损(用占位符引用,语法见标签)" style="width:100%"></textarea>
    </div>

    <div class="card">
      <button :disabled="busy.run" @click="runWf">{{busy.run?'运行中…(数十秒)':'▶ 运行工作流'}}</button>
      <button class="ghost" style="margin-left:8px" @click="toggleHist">🕘 运行历史</button>
      <span v-if="err" class="err" style="margin-left:12px">{{err}}</span>
    </div>

    <div v-if="hist.show" class="card">
      <h3>运行历史 <span style="color:var(--muted);font-weight:400;font-size:12px">{{curId?'当前工作流':'全部'}} · 点击查看结果</span>
        <button class="ghost" style="float:right" @click="hist.all=!hist.all;loadHist()">{{hist.all?'看当前':'看全部'}}</button></h3>
      <table v-if="hist.rows&&hist.rows.length"><thead><tr><th>时间</th><th>工作流</th><th>参数</th><th>分析师</th><th>结论预览</th></tr></thead>
        <tbody><tr v-for="r in hist.rows" :key="r.id" @click="viewRun(r.id)" style="cursor:pointer">
          <td style="white-space:nowrap">{{(r.created_at||'').slice(0,16)}}</td><td>{{r.name}}</td>
          <td>{{paramStr(r.params)}}</td><td>{{r.n_analysts}}</td>
          <td style="color:var(--muted)">{{r.final_preview}}…</td>
        </tr></tbody></table>
      <div v-else class="loading">暂无运行历史。</div>
    </div>

    <div v-if="busy.run" class="loading">AI 工作流执行中:取数 → 各分析师并行 → 综合…</div>
    <div v-if="res">
      <div class="card" style="padding:8px 14px">
        <span style="color:var(--muted);font-size:12px">结果:</span>
        <button class="ghost" @click="copyResult">📋 复制</button>
        <button class="ghost" @click="exportMd">📄 导出MD</button>
        <span v-if="res.data_keys&&res.data_keys.length" style="color:var(--muted);font-size:12px;margin-left:10px">数据块: {{res.data_keys.map(provName).join(' · ')}}</span>
      </div>
      <div v-for="a in res.analysts" :key="a.name" class="card">
        <h3>🔹 {{a.name}} <span style="color:var(--muted);font-weight:400;font-size:12px">{{a.provider}}</span></h3>
        <div class="md" v-html="md(a.output)"></div>
      </div>
      <div v-if="res.final" class="card" style="border-left:3px solid var(--accent)">
        <h3>🎯 综合结论 <span style="color:var(--muted);font-weight:400;font-size:12px">{{res.provider}}</span></h3>
        <div class="md" v-html="md(res.final)"></div>
      </div>
    </div>
  </div>`,
  setup(){
    const providers = ref([])
    const saved = ref([])
    const curId = ref(null)
    const err = ref('')
    const busy = reactive({ save:false, run:false })
    const run = reactive({ code:'600519', query:'' })
    const res = ref(null)
    const blank = () => ({ name:'新工作流', scope:'stock', description:'', data:[], analysts:[], synthesizer:{name:'综合',system:'',model:'',user:''} })
    const wf = reactive(blank())
    const scopes = SCOPES
    const ph = { code:'{{ctx.code}}', dataEx:'{{data.chan}}', analystEx:'{{analyst.分析师名}}' }
    const needCode = computed(()=> wf.scope==='stock' || wf.scope==='fund')
    const filteredProviders = computed(()=> providers.value.filter(p=> p.scope===wf.scope || p.scope==='market' || (wf.scope==='portfolio')))
    const provName = k => (providers.value.find(p=>p.key===k)||{}).name || k

    function applyCfg(w){
      const c = w.config||{}
      wf.name=w.name; wf.scope=w.scope||c.scope||'stock'; wf.description=w.description||''
      wf.data=[...(c.data||[])]
      wf.analysts=(c.analysts||[]).map(a=>({name:a.name||'分析',model:a.model||'',system:a.system||'',user:a.user||'',inputs:[...(a.inputs||[])]}))
      const s=c.synthesizer||{}; wf.synthesizer={name:s.name||'综合',model:s.model||'',system:s.system||'',user:s.user||''}
    }
    function loadSel(){ res.value=null; const w=saved.value.find(x=>x.id===curId.value); if(w) applyCfg(w); else Object.assign(wf, blank()) }
    function newWf(){ curId.value=null; Object.assign(wf, blank()); res.value=null }
    function addAnalyst(){ wf.analysts.push({name:'分析师'+(wf.analysts.length+1),model:'',system:'',user:'',inputs:[]}) }
    function buildConfig(){ return { scope:wf.scope, params: needCode.value?['code']:[], data:wf.data,
      analysts:wf.analysts.map(a=>({name:a.name,model:a.model||undefined,system:a.system,user:a.user,inputs:a.inputs})),
      synthesizer:{name:wf.synthesizer.name,model:wf.synthesizer.model||undefined,system:wf.synthesizer.system,user:wf.synthesizer.user} } }
    async function saveWf(){
      if(!wf.name.trim()){ err.value='请填名称'; return }
      busy.save=true; err.value=''
      try{ const r=await api('/api/workflow/save',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({id:curId.value, name:wf.name, scope:wf.scope, description:wf.description, config:buildConfig()})})
        await loadList(); curId.value=r.id }
      catch(e){ err.value=''+e }finally{ busy.save=false }
    }
    async function delWf(){
      if(!curId.value) return
      try{ await api('/api/workflow/'+curId.value,{method:'DELETE'}); curId.value=null; Object.assign(wf,blank()); await loadList() }
      catch(e){ err.value=''+e }
    }
    async function runWf(){
      busy.run=true; err.value=''; res.value=null
      const params={}; if(needCode.value) params.code=run.code; if(run.query) params.query=run.query
      try{ const r=await api('/api/workflow/run',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({config:buildConfig(), params, workflow_id:curId.value, name:wf.name})})
        res.value=r }
      catch(e){ err.value=''+e }finally{ busy.run=false }
    }
    async function loadList(){ try{ saved.value=await api('/api/workflow/list') }catch(e){} }
    const hist = reactive({ show:false, all:true, rows:null })
    const paramStr = p => Object.entries(p||{}).map(([k,v])=>k+'='+v).join(' ') || '—'
    async function loadHist(){
      const wid = (!hist.all && curId.value) ? curId.value : 0
      try{ hist.rows = await api('/api/workflow/runs?workflow_id='+wid) }catch(e){ hist.rows=[] }
    }
    function toggleHist(){ hist.show=!hist.show; if(hist.show) loadHist() }
    async function viewRun(id){
      try{ const r = await api('/api/workflow/run/'+id); if(r&&r.result){ res.value=r.result; window.scrollTo(0,document.body.scrollHeight) } }
      catch(e){ err.value=''+e }
    }
    const md = mdLite
    const phData = k => '{{data.'+k+'}}'
    function resultText(){
      if(!res.value) return ''
      let t = '# '+wf.name+'\n'
      if(res.value.ctx&&res.value.ctx.code) t += '代码: '+res.value.ctx.code+'\n'
      t += '\n'
      for(const a of (res.value.analysts||[])) t += '## '+a.name+'\n'+a.output+'\n\n'
      if(res.value.final) t += '## 综合结论\n'+res.value.final+'\n'
      return t
    }
    async function copyResult(){ try{ await navigator.clipboard.writeText(resultText()); err.value='已复制到剪贴板'; setTimeout(()=>err.value='',1500) }catch(e){ err.value='复制失败' } }
    function exportMd(){
      const blob=new Blob([resultText()],{type:'text/markdown;charset=utf-8'})
      const a=document.createElement('a'); a.href=URL.createObjectURL(blob)
      a.download=(wf.name||'workflow')+'_'+(run.code||'')+'.md'; a.click(); URL.revokeObjectURL(a.href)
    }
    async function saveAsNew(){
      const nm=prompt('另存为新工作流,名称:', wf.name+' 副本'); if(!nm) return
      busy.save=true; err.value=''
      try{ const r=await api('/api/workflow/save',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({id:null, name:nm, scope:wf.scope, description:wf.description, config:buildConfig()})})
        await loadList(); curId.value=r.id; wf.name=nm }
      catch(e){ err.value=''+e }finally{ busy.save=false }
    }
    const prev = reactive({ loading:false, data:null })
    async function previewData(){
      prev.loading=true; prev.data=null
      try{ prev.data = await api('/api/workflow/preview-data?keys='+wf.data.join(',')+'&code='+(run.code||'')+'&query='+encodeURIComponent(run.query||'')) }
      catch(e){ err.value=''+e }finally{ prev.loading=false }
    }
    const prevStr = v => { try{ return (typeof v==='string'? v : JSON.stringify(v,null,1)).slice(0,1500) }catch(e){ return String(v) } }
    onMounted(async ()=>{
      try{ providers.value=await api('/api/workflow/providers') }catch(e){}
      await loadList()
      if(saved.value.length){ curId.value=saved.value[0].id; loadSel() }
    })
    return { providers, saved, curId, err, busy, run, res, wf, scopes, ph, needCode, filteredProviders, provName,
             loadSel, newWf, addAnalyst, saveWf, delWf, runWf,
             hist, paramStr, loadHist, toggleHist, viewRun,
             md, phData, copyResult, exportMd, saveAsNew, prev, previewData, prevStr }
  }
}

import { reactive, ref, computed, onMounted, onUnmounted } from 'vue'
import { api } from '../lib.js'

export default {
  template: `
  <div>
    <div class="h1">⚙️ 设置</div>
    <div class="tabs">
      <div class="tab" :class="{active:tab==='jobs'}" @click="tab='jobs'">定时任务</div>
      <div class="tab" :class="{active:tab==='env'}" @click="tab='env'">环境配置</div>
    </div>

    <!-- 定时任务 -->
    <div v-if="tab==='jobs'">
      <p class="sub">后台自动化任务开关与运行观测。手动任务异步执行，刷新页面后仍可继续查看状态。</p>
      <div v-if="st.err" class="err">{{st.err}}</div>
      <div v-if="st.loading" class="loading">加载中…</div>
      <div class="card" style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <span class="pill">任务 {{st.jobs.length}}</span>
        <span class="pill" style="color:var(--green)">正常 {{summary.success}}</span>
        <span class="pill" style="color:var(--amber)">运行中 {{summary.running}}</span>
        <span class="pill" style="color:var(--red)">异常 {{summary.failed}}</span>
        <span v-if="summary.disabledCore" class="pill" style="color:var(--red)">
          核心关闭 {{summary.disabledCore}}
        </span>
        <button class="ghost" style="margin-left:auto;padding:3px 10px" @click="loadJobs"
                :disabled="st.loading">↻ 刷新</button>
      </div>
      <div v-for="cat in cats" :key="cat" class="card">
        <h3>{{cat}}</h3>
        <table><thead><tr><th>任务</th><th>计划</th><th>最近状态</th><th>说明</th><th style="text-align:center">启用</th><th style="text-align:center">手动</th></tr></thead>
          <tbody>
            <tr v-for="j in byCat(cat)" :key="j.name">
              <td><b>{{j.cn}}</b><span v-if="j.core" class="pill" style="margin-left:6px;font-size:10px">核心</span>
                  <div style="color:var(--muted);font-size:11px">{{j.name}}</div></td>
              <td>{{j.schedule}}</td>
              <td style="white-space:nowrap">
                <span class="pill" :style="{color:statusColor(effectiveRun(j)?.status)}">
                  {{statusText(effectiveRun(j)?.status)}}
                </span>
                <div style="color:var(--muted);font-size:11px;margin-top:3px">
                  {{fmtTime(effectiveRun(j)?.finished_at || effectiveRun(j)?.started_at || effectiveRun(j)?.requested_at)}}
                </div>
                <div v-if="effectiveRun(j)?.error" class="err"
                     :title="effectiveRun(j).error"
                     style="max-width:210px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:11px">
                  {{effectiveRun(j).error}}
                </div>
              </td>
              <td style="text-align:left;color:var(--muted);max-width:420px">{{j.description}}</td>
              <td style="text-align:center">
                <label class="sw">
                  <input type="checkbox" :checked="j.enabled" @change="toggle(j, $event.target.checked)"/>
                  <span class="sl"></span>
                </label>
              </td>
              <td style="text-align:center">
                <button class="ghost" style="padding:2px 8px;font-size:12px"
                        :title="j.trigger_note || ''"
                        :disabled="isRunning(j) || !j.triggerable" @click="runNow(j)">
                  {{!j.triggerable?'—':isRunning(j)?'运行中…':'▶ 运行'}}
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="card" style="color:var(--muted);font-size:12px">
        ⚠️ 这些开关只控制"是否执行"。后台调度器(jobs_hub)需在服务器常驻运行(看门狗/ systemd);
        Windows 本机默认不跑后台任务。
      </div>
    </div>

    <!-- 环境配置(.env) -->
    <div v-if="tab==='env'">
      <p class="sub">编辑 .env 环境变量(API key / 数据库 / 数据源 / 通知)。密钥只显示是否已设置，不回显内容；留空表示不修改。</p>
      <div v-if="e.err" class="err">{{e.err}}</div>
      <div v-if="e.msg" class="ok-msg">{{e.msg}}</div>
      <div v-if="e.loading" class="loading">加载中…</div>
      <div v-for="g in groups" :key="g" class="card">
        <h3>{{g}}</h3>
        <div v-for="f in byGroup(g)" :key="f.key" class="env-row">
          <div class="env-label">
            <b>{{f.label}}</b> <span style="color:var(--muted);font-size:11px">{{f.key}}</span>
            <div v-if="f.help" style="color:var(--muted);font-size:11px;margin-top:2px">{{f.help}}</div>
          </div>
          <div class="env-ctrl">
            <select v-if="f.type==='bool'" v-model="form[f.key]">
              <option value="true">开启</option><option value="false">关闭</option>
            </select>
            <input v-else-if="f.type==='secret'" type="password" v-model="form[f.key]"
                   :placeholder="f.set ? ('已设置 '+f.hint+'(留空不改)') : '未设置'"/>
            <input v-else v-model="form[f.key]" :type="f.type==='int'?'number':'text'"/>
          </div>
        </div>
      </div>
      <div class="card">
        <button :disabled="e.saving" @click="save">{{e.saving?'保存中…':'💾 保存到 .env'}}</button>
        <span style="color:var(--muted);font-size:12px;margin-left:12px">
          ⚠️ 部分配置(已加载的模块)需重启服务进程才完全生效。</span>
      </div>
    </div>
  </div>`,
  setup(){
    const tab = ref('jobs')
    // —— 定时任务 ——
    const st = reactive({ jobs:[], err:'', loading:false })
    const cats = computed(()=> [...new Set(st.jobs.map(j=>j.category))])
    const byCat = c => st.jobs.filter(j=>j.category===c)
    const finalOk = new Set(['success','skipped'])
    const finalBad = new Set(['error','failed','timeout','degraded','partial','interrupted'])
    const effectiveRun = j => {
      const m=j.manual_run, s=j.last_run
      if(m && ['queued','running'].includes(m.status)) return m
      const mt=m && (m.finished_at || m.started_at || m.requested_at)
      const st=s && (s.finished_at || s.started_at)
      return mt && (!st || String(mt)>=String(st)) ? m : s
    }
    const summary = computed(()=>{
      let success=0, runningN=0, failed=0, disabledCore=0
      st.jobs.forEach(j=>{
        const status=effectiveRun(j)?.status
        if(finalOk.has(status)) success++
        else if(['queued','running'].includes(status)) runningN++
        else if(finalBad.has(status)) failed++
        if(j.core && !j.enabled) disabledCore++
      })
      return {success,running:runningN,failed,disabledCore}
    })
    const statusText = s => ({
      success:'成功', skipped:'跳过', queued:'排队中', running:'运行中',
      error:'失败', failed:'失败', timeout:'超时', degraded:'降级', partial:'部分成功',
      interrupted:'已中断'
    }[s] || '暂无记录')
    const statusColor = s => finalOk.has(s)?'var(--green)':
      ['queued','running'].includes(s)?'var(--amber)':finalBad.has(s)?'var(--red)':'var(--muted)'
    const fmtTime = v => v ? String(v).slice(0,19).replace('T',' ') : '—'
    async function loadJobs(){
      st.loading=true; st.err=''
      try{ st.jobs = await api('/api/jobs') }catch(e){ st.err=''+e }finally{ st.loading=false }
    }
    async function toggle(j, on){
      try{ await api('/api/jobs/'+j.name+'/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({on})}); j.enabled=on }
      catch(e){ st.err=''+e; await loadJobs() }
    }
    // 手动异步触发；run_id 持久化，轮询结束后刷新任务状态。
    const running = reactive({})
    const timers = new Set()
    const isRunning = j => Boolean(running[j.name]) ||
      ['queued','running'].includes(j.manual_run?.status)
    function later(fn, ms){
      const id=setTimeout(()=>{ timers.delete(id); fn() }, ms); timers.add(id)
    }
    async function pollRun(j, runId){
      try{
        const run = await api('/api/task-runs/'+runId)
        j.manual_run = run
        if(['queued','running'].includes(run.status)){
          later(()=>pollRun(j,runId), 2500)
        }else{
          delete running[j.name]
          await loadJobs()
        }
      }catch(e){
        st.err=''+e; delete running[j.name]
      }
    }
    async function runNow(j){
      running[j.name] = true; st.err=''
      try{
        const r=await api('/api/jobs/'+j.name+'/run',{method:'POST'})
        running[j.name]=r.run_id
        j.manual_run={...r,requested_at:new Date().toISOString()}
        later(()=>pollRun(j,r.run_id), Math.max(1,r.poll_after_seconds||2)*1000)
      }catch(e){ st.err=''+e; delete running[j.name] }
    }
    // —— 环境配置 ——
    const e = reactive({ items:[], err:'', msg:'', loading:false, saving:false })
    const form = reactive({})
    const groups = computed(()=> [...new Set(e.items.map(i=>i.group))])
    const byGroup = g => e.items.filter(i=>i.group===g)
    async function loadEnv(){
      e.loading=true; e.err=''; e.msg=''
      try{
        e.items = await api('/api/env')
        e.items.forEach(i=>{ form[i.key] = i.type==='secret' ? '' : (i.value||'') })
      }catch(err){ e.err=''+err }finally{ e.loading=false }
    }
    async function save(){
      e.saving=true; e.err=''; e.msg=''
      // 只提交与原值不同的(secret 非空即提交,空跳过由后端处理)
      const updates = {}
      e.items.forEach(i=>{
        const v = form[i.key]
        if(i.type==='secret'){ if(v) updates[i.key]=v }
        else if((v||'') !== (i.value||'')) updates[i.key]=v
      })
      if(!Object.keys(updates).length){ e.msg='无改动'; e.saving=false; return }
      try{
        const r = await api('/api/env',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({updates})})
        e.msg = `已保存 ${r.changed.length} 项`+(r.changed.length?'：'+r.changed.join(', '):'')
        await loadEnv()
      }catch(err){ e.err=''+err }finally{ e.saving=false }
    }
    onMounted(()=>{ loadJobs(); loadEnv() })
    onUnmounted(()=>{ timers.forEach(clearTimeout); timers.clear() })
    return { tab, st, cats, byCat, summary, effectiveRun, statusText, statusColor, fmtTime,
      toggle, running, isRunning, runNow, loadJobs, e, form, groups, byGroup, save }
  }
}

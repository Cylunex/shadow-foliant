import { reactive, ref, computed, onMounted } from 'vue'
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
      <p class="sub">后台自动化任务开关(jobs_hub)。改动即时存库;任务需 jobs_hub 常驻运行才会真正触发。</p>
      <div v-if="st.err" class="err">{{st.err}}</div>
      <div v-if="st.loading" class="loading">加载中…</div>
      <div v-for="cat in cats" :key="cat" class="card">
        <h3>{{cat}}</h3>
        <table><thead><tr><th>任务</th><th>计划</th><th>说明</th><th style="text-align:center">启用</th><th style="text-align:center">手动</th></tr></thead>
          <tbody>
            <tr v-for="j in byCat(cat)" :key="j.name">
              <td><b>{{j.cn}}</b><span v-if="j.core" class="pill" style="margin-left:6px;font-size:10px">核心</span>
                  <div style="color:var(--muted);font-size:11px">{{j.name}}</div></td>
              <td>{{j.schedule}}</td>
              <td style="text-align:left;color:var(--muted);max-width:420px">{{j.description}}</td>
              <td style="text-align:center">
                <label class="sw">
                  <input type="checkbox" :checked="j.enabled" @change="toggle(j, $event.target.checked)"/>
                  <span class="sl"></span>
                </label>
              </td>
              <td style="text-align:center">
                <button class="ghost" style="padding:2px 8px;font-size:12px"
                        :disabled="running[j.name]" @click="runNow(j)">
                  {{running[j.name]==='ok'?'✅':running[j.name]?'…':'▶'}}
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
      <p class="sub">编辑 .env 环境变量(API key / 数据库 / 数据源 / 通知)。密钥仅显示尾 4 位,留空表示不修改。</p>
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
    async function loadJobs(){
      st.loading=true; st.err=''
      try{ st.jobs = await api('/api/jobs') }catch(e){ st.err=''+e }finally{ st.loading=false }
    }
    async function toggle(j, on){
      try{ await api('/api/jobs/'+j.name+'/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({on})}); j.enabled=on }
      catch(e){ st.err=''+e; await loadJobs() }
    }
    // 手动立即触发(后台跑,无视开关);按钮短暂显示 … → ✅
    const running = reactive({})
    async function runNow(j){
      running[j.name] = true
      try{ await api('/api/jobs/'+j.name+'/run',{method:'POST'}); running[j.name]='ok'
           setTimeout(()=>{ running[j.name]=false }, 3000) }
      catch(e){ st.err=''+e; running[j.name]=false }
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
    return { tab, st, cats, byCat, toggle, running, runNow, e, form, groups, byGroup, save }
  }
}

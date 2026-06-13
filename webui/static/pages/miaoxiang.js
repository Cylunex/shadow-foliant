import { reactive, ref, computed } from 'vue'
import { api } from '../lib.js'

// 妙想(东财 AI)9 个技能 + 输入提示
const SKILLS = [
  { k:'stock_diagnosis', t:'个股诊断', ph:'如:300750 宁德时代 当前估值贵不贵、有什么风险' },
  { k:'ask',             t:'财经问答', ph:'如:什么是注册制?对市场有什么影响' },
  { k:'hotspot',         t:'热点解读', ph:'如:今天 A股 有哪些热点板块' },
  { k:'comparable',      t:'可比公司', ph:'如:比亚迪 的可比公司有哪些' },
  { k:'finance_search',  t:'资讯搜索', ph:'如:英伟达 最新财报要点' },
  { k:'macro_data',      t:'宏观数据', ph:'如:最新的 CPI、PMI 数据' },
  { k:'industry_report', t:'行业研报', ph:'如:半导体 行业近况与展望' },
  { k:'topic_report',    t:'主题研报', ph:'如:人工智能 投资主题梳理' },
  { k:'fund_diagnosis',  t:'基金诊断', ph:'如:110011 这只基金怎么样' },
]

export default {
  template: `
  <div>
    <div class="h1">🧠 妙想 · 东财 AI</div>
    <p class="sub">东方财富 AI 的"第二意见":个股/基金诊断、热点、可比、资讯、宏观、行业/主题研报。</p>
    <div class="card">
      <div class="tabs" style="flex-wrap:wrap">
        <div v-for="s in skills" :key="s.k" class="tab" :class="{active:cur===s.k}" @click="pick(s.k)">{{s.t}}</div>
      </div>
      <div class="row" style="margin-top:10px">
        <div style="flex:1;min-width:280px"><input v-model="q" @keyup.enter="run" :placeholder="ph" style="width:100%"/></div>
        <button :disabled="m.loading" @click="run">{{m.loading?'妙想中…':'提问'}}</button>
      </div>
      <p v-if="cur==='stock_diagnosis'||cur==='fund_diagnosis'" class="sub" style="margin:6px 0 0">提示:带上代码(如 600519 / 110011)结果更准。</p>
      <p class="sub" style="margin:8px 0 0">⚠️ 问句会发往东财服务器(合规自评)。{{m.demo?'当前用 demo key,易限流,建议在设置页配 EM_API_KEY。':''}}</p>
    </div>
    <div v-if="m.err" class="err">{{m.err}}</div>
    <div v-if="m.loading" class="loading">妙想 AI 思考中(数秒~十几秒)…</div>
    <div v-else-if="m.content" class="card">
      <h3>{{skillName}} · 结果</h3>
      <pre style="white-space:pre-wrap;color:var(--txt);margin:0;font:14px/1.7 inherit">{{m.content}}</pre>
    </div>
  </div>`,
  setup(){
    const m = reactive({ content:'', err:'', loading:false, demo:false })
    const cur = ref('stock_diagnosis')
    const q = ref('')
    const ph = computed(()=> (SKILLS.find(s=>s.k===cur.value)||{}).ph)
    const skillName = computed(()=> (SKILLS.find(s=>s.k===cur.value)||{}).t)
    function pick(k){ cur.value=k }
    async function run(){
      if(!q.value.trim()){ m.err='请输入问题'; return }
      m.loading=true; m.err=''; m.content=''
      try{
        const r = await api('/api/miaoxiang?skill='+cur.value+'&q='+encodeURIComponent(q.value))
        if(r.error){ m.err=r.error } else { m.content=r.content; m.demo=!!r.using_demo_key }
      }catch(e){ m.err=''+e }finally{ m.loading=false }
    }
    return { skills:SKILLS, cur, q, ph, skillName, pick, m, run }
  }
}

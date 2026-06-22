import { reactive, ref, onMounted } from 'vue'
import { api } from '../lib.js'

const RATINGS = ['', '买入', '持有', '卖出']
const RATING_LABELS = { '': '全部评级', '买入': '买入', '持有': '持有', '卖出': '卖出' }
const DIMS = [
  { k:'source', t:'来源' }, { k:'confidence', t:'信心度' },
  { k:'horizon', t:'持有周期' }, { k:'outcome', t:'了结方式' }, { k:'month', t:'月份' },
]

export default {
  template: `
  <div>
    <div class="h1">🕘 历史</div>
    <div class="tabs" style="margin-bottom:18px">
      <div class="tab" :class="{active:tab==='records'}" @click="tab='records'">📋 深度分析记录</div>
      <div class="tab" :class="{active:tab==='eval'}" @click="switchEval">⭐ AI推荐战绩</div>
      <div class="tab" :class="{active:tab==='usage'}" @click="switchUsage">🔢 Token用量</div>
    </div>

    <!-- 深度分析记录 -->
    <div v-if="tab==='records'">
      <div class="card">
        <div class="row" style="flex-wrap:wrap;gap:8px;align-items:flex-end">
          <div><label>股票代码</label><input v-model="r.code" placeholder="可选筛选" style="width:100px"/></div>
          <div><label>开始日期</label><input type="date" v-model="r.date_from" style="width:140px"/></div>
          <div><label>结束日期</label><input type="date" v-model="r.date_to" style="width:140px"/></div>
          <div><label>评级</label><select v-model="r.rating" style="width:110px"><option v-for="rt in RATINGS" :value="rt">{{RATING_LABELS[rt]}}</option></select></div>
          <button :disabled="r.loading" @click="doSearch">{{r.loading?'加载中…':'查询'}}</button>
        </div>
      </div>
      <div v-if="r.err" class="err">{{r.err}}</div>
      <div v-if="r.list.length" style="margin-top:14px">
        <table style="width:100%">
          <thead><tr>
            <th v-if="r.mode==='all'">代码</th><th>日期</th><th>股票</th><th>评级</th><th>目标价</th><th>止损</th><th>操作建议</th>
          </tr></thead>
          <tbody>
            <tr v-for="it in r.list" :key="it.id">
              <td v-if="r.mode==='all'">{{it.symbol||'—'}}</td>
              <td>{{it.date}}</td>
              <td>{{it.stock_name||it.symbol}}({{r.mode==='all'?it.symbol:r.code}})</td>
              <td><b :class="{red:it.rating==='买入',green:it.rating==='卖出'}">{{it.rating||'—'}}</b></td>
              <td>{{it.target_price||'—'}}</td>
              <td :title="it.stop_loss" style="max-width:80px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{it.stop_loss||'—'}}</td>
              <td style="max-width:400px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" :title="it.summary">{{it.summary||'—'}}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-if="!r.loading && r.searched && !r.list.length" class="sub" style="margin-top:12px">暂无记录</div>
    </div>

    <!-- AI推荐战绩 + 维度分桶 -->
    <div v-if="tab==='eval'">
      <p class="sub">盈利反馈环:统计 AI 推荐的真实胜率 / 平均收益 / 盈亏比(近 90 天)。可按维度分桶看"哪种推荐真能赚"。需生产 PG 数据。</p>
      <div class="row" style="gap:8px;align-items:flex-end;margin-bottom:10px">
        <div><label>分桶维度</label>
          <select v-model="h.dim" @change="loadDim" style="width:120px">
            <option v-for="d in DIMS" :value="d.k">{{d.t}}</option>
          </select>
        </div>
      </div>
      <div v-if="h.err" class="err">{{h.err}}(本地 SQLite 无 ai_recommendations 表属正常,生产 PG 下可用)</div>
      <div v-if="h.loading" class="loading">加载中…</div>
      <div v-if="h.data" class="card">
        <div class="metrics">
          <div class="metric"><div class="k">样本</div><div class="v">{{h.data.overall.sample}}</div></div>
          <div class="metric"><div class="k">综合得分</div><div class="v">{{h.data.overall.score}}</div></div>
          <div class="metric"><div class="k">等级</div><div class="v"><span class="grade">{{h.data.overall.grade}}</span></div></div>
        </div>
        <pre style="white-space:pre-wrap;color:var(--muted);margin-top:14px;font:13px/1.6 inherit">{{h.dimReport||h.data.report}}</pre>
      </div>
    </div>

    <!-- Token 用量 -->
    <div v-if="tab==='usage'">
      <p class="sub">LLM Token 用量遥测:多智能体每天/每环节烧了多少 token、走哪个 provider/model(近 {{u.days}} 天)。</p>
      <div class="row" style="gap:8px;align-items:flex-end;margin-bottom:10px">
        <div><label>区间(天)</label>
          <select v-model.number="u.days" @change="loadUsage" style="width:90px">
            <option :value="7">7</option><option :value="30">30</option><option :value="90">90</option>
          </select>
        </div>
      </div>
      <div v-if="u.err" class="err">{{u.err}}</div>
      <div v-if="u.loading" class="loading">加载中…</div>
      <div v-if="u.data" class="card">
        <div class="metrics">
          <div class="metric"><div class="k">调用次数</div><div class="v">{{u.data.totals.calls}}</div></div>
          <div class="metric"><div class="k">总 Token</div><div class="v">{{fmt(u.data.totals.total_tokens)}}</div></div>
          <div class="metric"><div class="k">输入</div><div class="v">{{fmt(u.data.totals.prompt_tokens)}}</div></div>
          <div class="metric"><div class="k">输出</div><div class="v">{{fmt(u.data.totals.completion_tokens)}}</div></div>
        </div>
        <div v-if="!u.data.totals.calls" class="sub" style="margin-top:12px">暂无用量数据(尚未发生 LLM 调用,或遥测表未建)。</div>
        <div v-if="u.data.by_model.length" class="row" style="gap:24px;flex-wrap:wrap;margin-top:16px;align-items:flex-start">
          <div style="flex:1;min-width:280px">
            <div class="sub" style="margin-bottom:6px">按模型</div>
            <table style="width:100%">
              <thead><tr><th>provider:model</th><th style="text-align:right">调用</th><th style="text-align:right">Token</th></tr></thead>
              <tbody><tr v-for="m in u.data.by_model" :key="m.model">
                <td style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{m.model}}</td>
                <td style="text-align:right">{{m.calls}}</td><td style="text-align:right">{{fmt(m.total_tokens)}}</td>
              </tr></tbody>
            </table>
          </div>
          <div style="flex:1;min-width:280px">
            <div class="sub" style="margin-bottom:6px">按环节(call_type)</div>
            <table style="width:100%">
              <thead><tr><th>环节</th><th style="text-align:right">调用</th><th style="text-align:right">Token</th></tr></thead>
              <tbody><tr v-for="c in u.data.by_call_type" :key="c.call_type">
                <td>{{c.call_type}}</td><td style="text-align:right">{{c.calls}}</td><td style="text-align:right">{{fmt(c.total_tokens)}}</td>
              </tr></tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  </div>`,
  setup(){
    const r = reactive({ code:'', list:[], err:'', loading:false, searched:false, mode:'all',
                         date_from:'', date_to:'', rating:'' })
    const tab = ref('records')
    const h = reactive({ data:null, err:'', loading:false, dim:'source', dimReport:'' })
    const u = reactive({ data:null, err:'', loading:false, days:30 })

    function fmt(n){ n = Number(n)||0; return n>=1000 ? n.toLocaleString('en-US') : ''+n }

    function _buildParams(){
      let qs = 'limit=50'
      if(r.date_from) qs += '&date_from='+encodeURIComponent(r.date_from)
      if(r.date_to) qs += '&date_to='+encodeURIComponent(r.date_to)
      if(r.rating) qs += '&rating='+encodeURIComponent(r.rating)
      return qs
    }

    async function doSearch(){
      r.loading=true; r.err=''; r.list=[]; r.searched=false
      try{
        const base = r.code
          ? '/api/stock/'+r.code+'/deep-analysis/history'
          : '/api/deep-analysis/history/all'
        r.mode = r.code ? 'code' : 'all'
        const data = await api(base+'?'+_buildParams())
        r.list = Array.isArray(data) ? data : []
        r.searched = true
      }catch(e){ r.err=''+e }finally{ r.loading=false }
    }

    async function switchEval(){
      tab.value = 'eval'
      if(!h.data && !h.loading){
        h.loading=true; h.err=''
        try{ h.data = await api('/api/history/eval') }catch(e){ h.err=''+e }finally{ h.loading=false }
      }
    }

    async function loadDim(){
      h.dimReport=''
      if(h.dim==='source') return  // 默认报告即按来源,直接复用 h.data.report
      try{
        const d = await api('/api/history/eval/by?dim='+h.dim+'&days=90')
        h.dimReport = d.report || ''
      }catch(e){ h.dimReport = '加载失败: '+e }
    }

    async function switchUsage(){
      tab.value = 'usage'
      if(!u.data && !u.loading) loadUsage()
    }

    async function loadUsage(){
      u.loading=true; u.err=''
      try{ u.data = await api('/api/llm/usage?days='+u.days) }
      catch(e){ u.err=''+e }finally{ u.loading=false }
    }

    onMounted(doSearch)
    return { tab, r, h, u, doSearch, switchEval, switchUsage, loadDim, loadUsage, fmt,
             RATINGS, RATING_LABELS, DIMS }
  }
}

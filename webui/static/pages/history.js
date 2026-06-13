import { reactive, ref, onMounted } from 'vue'
import { api } from '../lib.js'

const RATINGS = ['', '买入', '持有', '卖出']
const RATING_LABELS = { '': '全部评级', '买入': '买入', '持有': '持有', '卖出': '卖出' }

export default {
  template: `
  <div>
    <div class="h1">🕘 历史</div>
    <div class="tabs" style="margin-bottom:18px">
      <div class="tab" :class="{active:tab==='records'}" @click="tab='records'">📋 深度分析记录</div>
      <div class="tab" :class="{active:tab==='eval'}" @click="switchEval">⭐ AI推荐战绩</div>
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

    <!-- AI推荐战绩 (原功能) -->
    <div v-if="tab==='eval'">
      <p class="sub">盈利反馈环:按来源统计 AI 推荐的真实胜率 / 平均收益 / 盈亏比(近 90 天)。需生产 PG 数据。</p>
      <div v-if="h.err" class="err">{{h.err}}(本地 SQLite 无 ai_recommendations 表属正常,生产 PG 下可用)</div>
      <div v-if="h.loading" class="loading">加载中…</div>
      <div v-if="h.data" class="card">
        <div class="metrics">
          <div class="metric"><div class="k">样本</div><div class="v">{{h.data.overall.sample}}</div></div>
          <div class="metric"><div class="k">综合得分</div><div class="v">{{h.data.overall.score}}</div></div>
          <div class="metric"><div class="k">等级</div><div class="v"><span class="grade">{{h.data.overall.grade}}</span></div></div>
        </div>
        <pre style="white-space:pre-wrap;color:var(--muted);margin-top:14px;font:13px/1.6 inherit">{{h.data.report}}</pre>
      </div>
    </div>
  </div>`,
  setup(){
    const r = reactive({ code:'', list:[], err:'', loading:false, searched:false, mode:'all',
                         date_from:'', date_to:'', rating:'' })
    const tab = ref('records')
    const h = reactive({ data:null, err:'', loading:false })

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

    onMounted(doSearch)
    return { tab, r, h, doSearch, switchEval, RATINGS, RATING_LABELS }
  }
}

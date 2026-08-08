import { reactive, onMounted } from 'vue'
import { api } from '../lib.js'

const STATUS_CN = {
  success:'正常', degraded:'降级', partial:'部分成功', stale:'过期',
  failed:'失败', missing:'缺失', running:'运行中', queued:'排队中',
  skipped:'已跳过', interrupted:'已中断',
}

export default {
  template: `
  <div>
    <div class="h1">🕹️ Agent 驾驶舱</div>
    <p class="sub">只读汇总任务、数据质量、选股产物、推荐与决策信号；不会触发重分析。</p>
    <div v-if="s.err" class="err">{{s.err}}</div>
    <div v-if="s.loading" class="loading">加载中…</div>
    <template v-if="s.res">
      <div class="card" style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <span class="pill" :style="{color:s.res.status==='success'?'var(--green)':'var(--amber)'}">
          {{STATUS_CN[s.res.status]||s.res.status}}
        </span>
        <span class="pill">持仓 {{d.holding_count??'—'}}</span>
        <span class="pill">活跃推荐 {{d.active_recommendation_count??'—'}}</span>
        <span class="pill">活跃信号 {{d.active_signal_count??'—'}}</span>
        <span class="pill">任务 {{d.tasks?.total??'—'}}</span>
        <button class="ghost" style="margin-left:auto;padding:3px 10px" @click="load">↻ 刷新</button>
      </div>

      <div v-if="s.res.meta?.warnings?.length" class="card">
        <h3>⚠️ 当前警告</h3>
        <div v-for="(w,i) in s.res.meta.warnings" :key="i" style="margin:5px 0">{{w}}</div>
      </div>

      <div class="card">
        <h3>🏆 最终优选 · {{d.selection?.meta?.snapshot_date||'暂无日期'}}</h3>
        <table v-if="d.selection?.data?.final_rows?.length">
          <thead><tr><th>#</th><th>代码</th><th>名称</th><th>优选分</th><th>红蓝</th><th>优选依据</th></tr></thead>
          <tbody><tr v-for="(r,i) in d.selection.data.final_rows" :key="r.code">
            <td>{{i+1}}</td><td>{{r.code}}</td><td>{{r.name}}</td><td>{{r.final_score}}</td>
            <td>{{r.debate_verdict||'待AI'}}</td><td style="text-align:left">{{r.final_reason}}</td>
          </tr></tbody>
        </table>
        <div v-else class="loading">暂无最终优选；下一次综合选股后生成。</div>
      </div>

      <div class="card">
        <h3>🎯 综合选股 TOP15</h3>
        <table v-if="d.selection?.data?.rows?.length">
          <thead><tr><th>#</th><th>代码</th><th>名称</th><th>评分</th><th>现价</th><th>涨跌%</th><th>红蓝</th><th>来源</th></tr></thead>
          <tbody><tr v-for="r in d.selection.data.rows" :key="r.code">
            <td>{{r.rank}}</td><td>{{r.code}}</td><td>{{r.name}}</td><td>{{r.score}}</td>
            <td>{{r.price??'—'}}</td><td :class="(r.change_pct||0)>=0?'red':'green'">{{r.change_pct??'—'}}</td>
            <td>{{r.debate_verdict||'—'}}</td><td style="text-align:left">{{(r.sources||[]).join(' / ')||'—'}}</td>
          </tr></tbody>
        </table>
        <div v-else class="loading">暂无结构化选股产物；下一次 unified_selection 后生成。</div>
      </div>

      <div class="row stretch">
        <div class="card flex1">
          <h3>异常任务</h3>
          <div v-for="r in d.tasks?.failed_recent||[]" :key="r.name" style="margin:7px 0">
            <b>{{r.name}}</b> · <span class="red">{{r.status}}</span>
            <div class="sub" style="margin:2px 0">{{r.error||'无错误详情'}}</div>
          </div>
          <div v-if="!d.tasks?.failed_recent?.length" class="loading">最近任务无异常。</div>
        </div>
        <div class="card flex1">
          <h3>运行中的手动任务</h3>
          <div v-for="r in d.tasks?.running_manual||[]" :key="r.run_id" style="margin:7px 0">
            <b>{{r.task_name}}</b> · {{r.status}} · 第 {{r.attempts||0}} 次
            <div class="sub" style="margin:2px 0">{{r.run_id}}</div>
          </div>
          <div v-if="!d.tasks?.running_manual?.length" class="loading">暂无。</div>
        </div>
      </div>

      <div class="row stretch">
        <div class="card flex1">
          <h3>活跃推荐</h3>
          <div v-for="r in d.active_recommendations||[]" :key="r.id" style="margin:7px 0">
            <b>{{r.symbol}} {{r.name}}</b> · {{r.rating}} · {{r.source}}
          </div>
          <div v-if="!d.active_recommendations?.length" class="loading">暂无。</div>
        </div>
        <div class="card flex1">
          <h3>活跃决策信号</h3>
          <div v-for="r in d.active_signals||[]" :key="r.id" style="margin:7px 0">
            <b>{{r.code}} {{r.name}}</b> · {{r.action_cn||r.action}} · {{r.source_type}}
          </div>
          <div v-if="!d.active_signals?.length" class="loading">暂无。</div>
        </div>
      </div>
    </template>
  </div>`,
  setup(){
    const s=reactive({res:null,loading:false,err:''})
    const d=new Proxy({}, {get(_t,k){ return s.res?.data?.[k] }})
    async function load(){
      s.loading=true;s.err=''
      try{s.res=await api('/api/agent/cockpit')}catch(e){s.err=''+e}finally{s.loading=false}
    }
    onMounted(load)
    return {s,d,load,STATUS_CN}
  }
}

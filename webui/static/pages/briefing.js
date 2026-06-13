import { reactive, onMounted } from 'vue'
import { api, mdLite, cls, fmt } from '../lib.js'

export default {
  template: `
  <div>
    <div class="h1">☀️ 晨报</div>
    <p class="sub">一眼看完:大盘 · 今日买入候选 · 持仓该卖的 · 持仓买卖点。每日 09:00 自动生成,全天秒开;点刷新可强制重算。</p>
    <div class="card" style="padding:8px 14px">
      <button :disabled="b.loading" @click="load(true)">{{b.loading?'生成中…(首次扫描持仓约1分钟)':'↻ 刷新晨报'}}</button>
      <button class="ghost" style="margin-left:8px" :disabled="ai.loading||!b.data" @click="aiSummary">{{ai.loading?'总结中…':'🤖 AI一句话提示'}}</button>
      <span v-if="b.err" class="err" style="margin-left:10px">{{b.err}}</span>
    </div>

    <div v-if="ai.text" class="card" style="border-left:3px solid var(--accent)">
      <div class="md" v-html="md(ai.text)"></div>
    </div>

    <div v-if="b.loading&&!b.data" class="loading">晨报生成中:取大盘 → 多因子选股 → 逐只扫描持仓…</div>
    <div v-if="b.data&&b.data._warming" class="card" style="border-left:3px solid var(--amber)">
      ⏳ 今日晨报尚未生成(每日 09:00 定时自动生成;盘中生成后全天秒开)。需要现在看就点上方「↻ 刷新晨报」生成一次(约1分钟)。
    </div>

    <template v-if="b.data&&!b.data._warming">
      <!-- 大盘 -->
      <div class="card">
        <h3>📊 大盘速览</h3>
        <div style="display:flex;flex-wrap:wrap;gap:18px;align-items:center">
          <div v-for="x in b.data.market.indices" :key="x.name"><span style="color:var(--muted);font-size:12px">{{x.name}}</span>
            <b :class="x.v&&x.v.includes('-')?'green':'red'" style="margin-left:5px">{{x.v}}</b></div>
        </div>
        <div style="margin-top:10px;font-size:13px">
          <span style="color:var(--muted)">强势板块:</span>
          <span v-for="s in b.data.market.sector_top" :key="s.板块" style="margin-right:12px"><b class="red">{{s.板块}} {{s.涨跌幅}}%</b> <span style="color:var(--muted)">{{s.领涨}}</span></span>
        </div>
        <div style="margin-top:4px;font-size:13px"><span style="color:var(--muted)">弱势板块:</span>
          <span v-for="s in b.data.market.sector_bottom" :key="s.板块" style="margin-right:12px"><b class="green">{{s.板块}} {{s.涨跌幅}}%</b></span>
        </div>
      </div>

      <!-- 买入推荐 -->
      <div class="card">
        <h3>🟢 今日买入候选 <span style="color:var(--muted);font-weight:400;font-size:12px">沪深300多因子 Top</span></h3>
        <table v-if="b.data.buy&&b.data.buy.length"><thead><tr><th>代码</th><th>名称</th><th>现价</th><th>综合分</th></tr></thead>
          <tbody><tr v-for="x in b.data.buy" :key="x.code"><td>{{x.code}}</td><td>{{x.name||'—'}}</td><td>{{x.price?fmt(x.price):'—'}}</td><td>{{x.composite}}</td></tr></tbody></table>
        <div v-else class="loading">暂无(多因子数据未就绪)。</div>
        <p class="sub" style="margin:6px 0 0">仅横截面打分候选,买入前自行结合大盘与个股研判。</p>
      </div>

      <!-- 持仓卖出提示 -->
      <div class="card" style="border-left:3px solid var(--green)">
        <h3>🔴 持仓建议关注卖出 <span style="color:var(--muted);font-weight:400;font-size:12px">扫了 {{b.data.scanned}} 只持仓,按风险信号排序</span></h3>
        <div v-if="b.data.sell&&b.data.sell.length">
          <div v-for="s in b.data.sell" :key="s.code" style="padding:9px 0;border-bottom:1px solid var(--line)">
            <b>{{s.name}} <span style="color:var(--muted);font-weight:400">{{s.code}}</span></b>
            <span class="pill" style="margin-left:8px">风险分 {{s.sell_score}}</span>
            <div style="color:var(--muted);font-size:13px;margin-top:3px">{{s.sell_reasons.join(' · ')}}</div>
          </div>
        </div>
        <div v-else class="loading">持仓暂无明显卖出信号 👍</div>
      </div>

      <!-- 持仓买卖点 -->
      <div v-if="b.data.hold_buy&&b.data.hold_buy.length" class="card">
        <h3>🟡 持仓出现买点/企稳 <span style="color:var(--muted);font-weight:400;font-size:12px">可考虑加仓</span></h3>
        <div v-for="s in b.data.hold_buy" :key="s.code" style="padding:7px 0;border-bottom:1px solid var(--line)">
          <b>{{s.name}} <span style="color:var(--muted);font-weight:400">{{s.code}}</span></b>
          <span style="color:var(--muted);font-size:13px;margin-left:8px">{{s.buy_reason}}</span>
        </div>
      </div>
    </template>
  </div>`,
  setup(){
    const b = reactive({ data:null, err:'', loading:false })
    const ai = reactive({ text:'', loading:false })
    async function load(force){
      b.loading=true; b.err=''; if(force) ai.text=''
      try{ b.data = await api('/api/briefing/morning'+(force?'?force=1':'')) }
      catch(e){ b.err=''+e }finally{ b.loading=false }
      // 不自动刷新:冷态只显示提示,由用户点「刷新晨报」手动生成一次
    }
    async function aiSummary(){
      ai.loading=true
      try{ const r = await api('/api/briefing/ai-summary',{method:'POST'}); ai.text = r.error||r.analysis }
      catch(e){ ai.text='AI总结失败: '+e }finally{ ai.loading=false }
    }
    onMounted(()=> load(false))
    return { b, ai, load, aiSummary, md:mdLite, cls, fmt }
  }
}

import { reactive, computed } from 'vue'
import { api, cls } from '../lib.js'

export default {
  template: `
  <div>
    <div class="h1">⭐ AI 双层推荐</div>
    <p class="sub">把候选股池交给 AI,产出<b>短线</b>(动量/事件,评分≥70)与<b>长期</b>(基本面/估值,评分≥75)两档推荐。需 LLM key,耗 token。</p>
    <div class="card">
      <label>候选股池(代码,逗号/空格/换行分隔)</label>
      <textarea v-model="r.codesText" rows="3" placeholder="如 600519 000858 300750 002594" style="width:100%;margin-top:6px"></textarea>
      <div class="row" style="margin-top:10px">
        <button :disabled="r.loading" @click="run">{{r.loading?'AI 研判中…(数十秒)':'生成推荐'}}</button>
        <button class="ghost" :disabled="r.loading" @click="useHoldings">用我的持仓</button>
        <button class="ghost" :disabled="r.loading" @click="useHs300">用沪深300多因子Top</button>
        <span class="pill">{{codeCount}} 只候选</span>
      </div>
      <p class="sub" style="margin:8px 0 0">不达分数线 AI 不会硬塞;logic 引用候选池真实数字。最多取前 40 只。</p>
    </div>
    <div v-if="r.err" class="err">{{r.err}}</div>
    <div v-if="r.loading" class="loading">AI 双层研判中,请稍候…</div>
    <div v-else-if="r.res">
      <div v-if="r.res.error" class="err">{{r.res.error}}<span v-if="r.res.raw"> · 原始: {{r.res.raw.slice(0,200)}}</span></div>
      <template v-else>
        <div class="card" v-if="r.res.market_view||r.res.risk_warning">
          <div v-if="r.res.market_view"><b>市场判断:</b> {{r.res.market_view}}</div>
          <div v-if="r.res.risk_warning" style="margin-top:6px;color:var(--muted)"><b>风险提示:</b> {{r.res.risk_warning}}</div>
          <div style="margin-top:6px;font-size:12px;color:var(--muted)">模型: {{r.res.provider||'—'}}</div>
        </div>
        <div class="card">
          <h3>🚀 短线推荐 <span class="pill">{{(r.res.short_term||[]).length}}</span></h3>
          <div v-if="!(r.res.short_term||[]).length" class="loading">无达标短线标的。</div>
          <div v-for="(x,i) in r.res.short_term" :key="i" class="reco-item">
            <div class="reco-head">
              <b>{{x.name}} <span style="color:var(--muted);font-weight:400">{{x.code}}</span></b>
              <span class="reco-score">{{x.score}}分</span>
              <span class="pill">{{x.horizon}}</span>
              <span v-if="x.confidence" class="pill">信心 {{x.confidence}}</span>
              <span style="margin-left:auto">目标 <b class="red">+{{x.target_pct}}%</b> · 止损 <b class="green">{{x.stop_pct}}%</b></span>
            </div>
            <div class="reco-logic">{{x.logic}}</div>
            <div v-if="(x.risks||[]).length" class="reco-risk">风险: {{(x.risks||[]).join('; ')}}</div>
          </div>
        </div>
        <div class="card">
          <h3>🏛️ 长期推荐 <span class="pill">{{(r.res.long_term||[]).length}}</span></h3>
          <div v-if="!(r.res.long_term||[]).length" class="loading">无达标长期标的。</div>
          <div v-for="(x,i) in r.res.long_term" :key="i" class="reco-item">
            <div class="reco-head">
              <b>{{x.name}} <span style="color:var(--muted);font-weight:400">{{x.code}}</span></b>
              <span class="reco-score">{{x.score}}分</span>
              <span class="pill">{{x.horizon}}</span>
              <span v-if="x.confidence" class="pill">信心 {{x.confidence}}</span>
              <span style="margin-left:auto">目标 <b class="red">+{{x.target_pct}}%</b></span>
            </div>
            <div class="reco-logic">{{x.logic}}</div>
            <div v-if="(x.risks||[]).length" class="reco-risk">风险: {{(x.risks||[]).join('; ')}}</div>
          </div>
        </div>
      </template>
    </div>
  </div>`,
  setup(){
    const r = reactive({ codesText:'', res:null, err:'', loading:false })
    const parse = () => (r.codesText.match(/\d{6}/g) || [])
    const codeCount = computed(()=> parse().length)
    async function run(){
      const codes = parse()
      if(!codes.length){ r.err='请填至少一个6位代码'; return }
      r.loading=true; r.err=''; r.res=null
      try{ r.res = await api('/api/reco/dual-horizon',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({codes})}) }
      catch(e){ r.err=''+e }finally{ r.loading=false }
    }
    async function useHoldings(){
      try{ const h = await api('/api/portfolio/overview'); const codes=(h.stocks||h||[]).map(x=>x.code||x.symbol).filter(Boolean); r.codesText=codes.join(' ') }
      catch(e){ r.err='取持仓失败: '+e }
    }
    async function useHs300(){
      try{ const m = await api('/api/screen/multifactor?index=000300&n=20'); r.codesText=(m.top||[]).map(x=>x.symbol).join(' ') }
      catch(e){ r.err='取多因子失败: '+e }
    }
    return { r, codeCount, run, useHoldings, useHs300, cls }
  }
}
